#include "pgwire.hpp"

#include "engine.hpp"
#include "sql.hpp"

#include <algorithm>
#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <poll.h>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/time.h>
#include <utility>
#include <vector>

namespace datom {
namespace {

constexpr std::uint32_t protocol_v3 = 3u << 16;
constexpr std::uint32_t max_startup_size = 10000;
constexpr std::uint32_t max_request_size = 8 * 1024 * 1024;

struct Disconnected {};

// The reader shares one absolute deadline across the complete incoming frame.
// The writer applies SO_SNDTIMEO only while the client makes no progress.
using Clock = std::chrono::steady_clock;
using Deadline = Clock::time_point;

Deadline socket_deadline(int fd, int option) {
    timeval timeout{};
    socklen_t size = sizeof(timeout);
    if (::getsockopt(fd, SOL_SOCKET, option, &timeout, &size) < 0) throw Disconnected{};
    if (timeout.tv_sec == 0 && timeout.tv_usec == 0) return Deadline::max();
    return Clock::now() + std::chrono::seconds(timeout.tv_sec) +
           std::chrono::microseconds(timeout.tv_usec);
}

void await_io(int fd, short events, Deadline deadline) {
    pollfd wait{fd, events, 0};
    for (;;) {
        int milliseconds = -1;
        if (deadline != Deadline::max()) {
            const auto left = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now());
            if (left.count() <= 0) throw Disconnected{};
            const auto maximum = std::chrono::milliseconds(std::numeric_limits<int>::max());
            milliseconds = static_cast<int>(std::min(left, maximum).count());
        }
        const int ready = ::poll(&wait, 1, milliseconds);
        if (ready > 0) return;
        if (ready < 0 && errno == EINTR) continue;
        throw Disconnected{};
    }
}

void receive_exact(int fd, char* data, std::size_t remaining, Deadline deadline) {
    while (remaining > 0) {
        if (Clock::now() >= deadline) throw Disconnected{};
        const auto received = ::recv(fd, data, remaining, MSG_DONTWAIT);
        if (received < 0 && errno == EINTR) continue;
        if (received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            await_io(fd, POLLIN, deadline);
            continue;
        }
        if (received <= 0) throw Disconnected{};
        data += received;
        remaining -= static_cast<std::size_t>(received);
    }
}

void send_all(int fd, std::string_view data) {
    std::optional<Deadline> stalled_until;
    while (!data.empty()) {
        const auto sent = ::send(fd, data.data(), data.size(), MSG_DONTWAIT);
        if (sent < 0 && errno == EINTR) continue;
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            if (!stalled_until) stalled_until = socket_deadline(fd, SO_SNDTIMEO);
            await_io(fd, POLLOUT, *stalled_until);
            continue;
        }
        if (sent <= 0) throw Disconnected{};
        data.remove_prefix(static_cast<std::size_t>(sent));
        stalled_until.reset();
    }
}

void put16(std::string& dst, std::uint16_t value) {
    const auto net = htons(value);
    dst.append(reinterpret_cast<const char*>(&net), sizeof(net));
}

void put32(std::string& dst, std::uint32_t value) {
    const auto net = htonl(value);
    dst.append(reinterpret_cast<const char*>(&net), sizeof(net));
}

void put_string(std::string& dst, std::string_view value) {
    dst.append(value);
    dst.push_back('\0');
}

std::uint32_t get32(const char* data) {
    std::uint32_t net;
    std::memcpy(&net, data, sizeof(net));
    return ntohl(net);
}

std::string read_payload(int fd, std::uint32_t limit, Deadline deadline) {
    char header[4];
    receive_exact(fd, header, sizeof(header), deadline);
    const auto length = get32(header);
    if (length < 4 || length > limit)
        throw DbError("08P01", "invalid frontend message length");
    std::string payload(length - 4, '\0');
    receive_exact(fd, payload.data(), payload.size(), deadline);
    return payload;
}

// Coalesce result frames so large SELECTs don't require a separate send per row.
class Reply {
    int fd_;
    std::string buffer_;

public:
    explicit Reply(int fd) : fd_(fd) {}

    void frame(char type, std::string_view payload = {}) {
        if (payload.size() > std::numeric_limits<std::int32_t>::max() - 4u)
            throw DbError("54000", "response exceeds protocol message size");
        buffer_.push_back(type);
        put32(buffer_, static_cast<std::uint32_t>(4 + payload.size()));
        buffer_.append(payload);
        if (buffer_.size() >= 16384) flush();
    }

    void flush() {
        if (buffer_.empty()) return;
        send_all(fd_, buffer_);
        buffer_.clear();
    }

    void error(const DbError& err, bool fatal = false) {
        std::string payload;
        for (const char field : {'S', 'V'}) {
            payload.push_back(field);
            put_string(payload, fatal ? "FATAL" : "ERROR");
        }
        payload.push_back('C');
        put_string(payload, err.code);
        payload.push_back('M');
        put_string(payload, err.what());
        payload.push_back('\0');
        frame('E', payload);
    }

    void parameter(std::string_view key, std::string_view value) {
        std::string payload;
        put_string(payload, key);
        put_string(payload, value);
        frame('S', payload);
    }
};

std::string_view take_string(std::string_view& input) {
    const auto end = input.find('\0');
    if (end == std::string_view::npos)
        throw DbError("08P01", "unterminated protocol string");
    const auto value = input.substr(0, end);
    input.remove_prefix(end + 1);
    return value;
}

bool is_utf8_encoding(std::string_view name) {
    std::string folded(name);
    for (char& ch : folded)
        if (ch >= 'A' && ch <= 'Z') ch = static_cast<char>(ch + ('a' - 'A'));
    return folded == "utf8" || folded == "utf-8" || folded == "unicode";
}

bool startup(int fd, Reply& reply, SessionPhase& phase, Deadline deadline) {
    const std::string payload = read_payload(fd, max_startup_size, deadline);
    if (payload.size() < 4)
        throw DbError("08P01", "truncated startup packet");
    const auto version = get32(payload.data());
    if (version == 80877102) return false; // CancelRequest has no reply.
    if (version == 80877103 || version == 80877104) { // SSLRequest / GSSENCRequest
        if (payload.size() != 4)
            throw DbError("08P01", "invalid encryption request");
        send_all(fd, "N");
        return true;
    }
    if ((version >> 16) != 3)
        throw DbError("0A000", "Datom supports PostgreSQL protocol 3.0");

    std::string_view params(payload.data() + 4, payload.size() - 4);
    std::vector<std::string_view> unsupported;
    std::string_view user;
    std::string_view database;
    while (true) {
        const auto key = take_string(params);
        if (key.empty()) break;
        const auto value = take_string(params);
        if (key == "user") {
            user = value;
        } else if (key == "database") {
            database = value;
        } else if (key.substr(0, 5) == "_pq_.") {
            unsupported.push_back(key);
        } else if (key == "client_encoding") {
            if (!is_utf8_encoding(value))
                throw DbError("22023", "Datom supports UTF8 client encoding");
        } else if (key != "application_name" && (key != "options" || !value.empty())) {
            throw DbError("0A000", "unsupported startup option: " + std::string(key));
        }
    }
    if (!params.empty()) throw DbError("08P01", "trailing data in startup packet");
    if (user.empty()) throw DbError("28000", "startup requires a nonempty user");
    if (database.empty()) database = user;
    if (database != "datom") throw DbError("3D000", "only database datom exists");
    if (version != protocol_v3 || !unsupported.empty()) {
        std::string negotiation;
        put32(negotiation, protocol_v3);
        put32(negotiation, static_cast<std::uint32_t>(unsupported.size()));
        for (const auto option : unsupported) put_string(negotiation, option);
        reply.frame('v', negotiation);
    }
    reply.frame('R', std::string(4, '\0')); // AuthenticationOk (trust).
    reply.parameter("server_version", "0.1 (Datom)");
    reply.parameter("server_encoding", "UTF8");
    reply.parameter("client_encoding", "UTF8");
    reply.parameter("standard_conforming_strings", "on");
    reply.parameter("integer_datetimes", "on");
    reply.frame('Z', "I");
    phase = SessionPhase::Ready;
    return true;
}

void describe_columns(Reply& reply, const Row& columns) {
    std::string desc;
    put16(desc, static_cast<std::uint16_t>(columns.size()));
    for (const auto& column : columns) {
        const bool is_id = column == "id";
        put_string(desc, column);
        put32(desc, 0); // No PostgreSQL catalog OID for this table.
        put16(desc, 0);
        put32(desc, is_id ? 20 : 25); // INT8OID / TEXTOID.
        put16(desc, is_id ? 8 : 0xffff);
        put32(desc, 0xffffffff); // No type modifier.
        put16(desc, 0); // Text format.
    }
    reply.frame('T', desc);
}

void data_row(Reply& reply, const Row& row, std::string& data) {
    data.clear();
    put16(data, static_cast<std::uint16_t>(row.size()));
    constexpr std::size_t max_data = std::numeric_limits<std::int32_t>::max() - 4;
    for (const auto& value : row) {
        if (data.size() > max_data - 4 || value.size() > max_data - 4 - data.size())
            throw DbError("54000", "result row exceeds protocol message size");
        put32(data, static_cast<std::uint32_t>(value.size()));
        data.append(value);
    }
    reply.frame('D', data);
}

void simple_query(Reply& reply, Engine& engine, std::string_view payload) {
    const auto sql = take_string(payload);
    if (!payload.empty()) throw DbError("08P01", "trailing data in Query");
    try {
        std::optional<Statement> statement;
        for_each_statement(sql, [&](Statement&& parsed) {
            if (statement)
                throw DbError("0A000", "send one SQL statement per Query message");
            statement.emplace(std::move(parsed));
        });
        if (!statement) {
            reply.frame('I');
        } else {
            if (statement->columns.size() > std::numeric_limits<std::int16_t>::max())
                throw DbError("54000", "too many columns for the wire protocol");
            std::string data;
            const ResultSink sink{
                [&](const Row& columns) { describe_columns(reply, columns); },
                [&](const Row& row) { data_row(reply, row, data); }
            };
            std::string complete;
            put_string(complete, engine.execute(*statement, &sink).tag);
            reply.frame('C', complete);
        }
    } catch (const DbError& err) {
        reply.error(err);
    }
    reply.frame('Z', "I");
}

bool process_message(int fd, Engine& engine, SessionPhase& phase, Reply& reply) {
    const auto deadline = socket_deadline(fd, SO_RCVTIMEO);
    if (phase == SessionPhase::Startup) return startup(fd, reply, phase, deadline);
    char type;
    receive_exact(fd, &type, 1, deadline);
    const auto payload = read_payload(fd, max_request_size, deadline);
    if ((type == 'X' || type == 'S' || type == 'H') && !payload.empty())
        throw DbError("08P01", "unexpected payload in control message");
    if (type == 'X') return false;
    if (type == 'S') {
        phase = SessionPhase::Ready;
        reply.frame('Z', "I");
    } else if (phase == SessionPhase::AwaitSync || type == 'H') {
        // PostgreSQL discards extended-query messages after an error until Sync.
    } else if (type == 'Q') {
        simple_query(reply, engine, payload);
    } else if (std::string_view("PBDCE").find(type) != std::string_view::npos) {
        reply.error(DbError("0A000", "use the PostgreSQL Simple Query protocol"));
        phase = SessionPhase::AwaitSync;
    } else {
        throw DbError("08P01", "unexpected frontend message type");
    }
    return true;
}

} // namespace

bool serve_message(int fd, Engine& engine, SessionPhase& phase) {
    try {
        Reply reply(fd);
        bool keep = false;
        try {
            keep = process_message(fd, engine, phase, reply);
        } catch (const DbError& err) {
            reply.error(err, true);
        }
        reply.flush();
        return keep;
    } catch (const Disconnected&) {
        return false;
    }
}

} // namespace datom
