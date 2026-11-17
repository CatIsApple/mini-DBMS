#include "engine.hpp"
#include "bptree.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <cstdint>
#include <cstring>
#include <limits>
#include <mutex>
#include <shared_mutex>
#include <system_error>

#include <fcntl.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace datom {
namespace {

// Little-endian disk format. Each INSERT appends exactly one frame:
// "DTX1", payload length, row count, payload checksum, header checksum,
// packed rows, "CMT1". A row is (u64 id, u32 first field size, u32 second field size, bytes).
constexpr std::string_view frame_magic = "DTX1";
constexpr std::string_view commit_marker = "CMT1";
constexpr std::size_t header_size = 20;
constexpr std::size_t row_header_size = 16;
constexpr std::size_t max_payload = 64 * 1024 * 1024;
constexpr std::uint64_t max_id = std::numeric_limits<std::int64_t>::max();

[[noreturn]] void io_error(const char* operation) {
    int error = errno;
    throw DbError("58030", std::string(operation) + ": " +
                             std::error_code(error, std::generic_category()).message());
}

[[noreturn]] void corrupt(std::size_t offset) {
    throw DbError("XX001", "invalid table data at byte " + std::to_string(offset));
}

class File {
public:
    explicit File(int fd) : fd_(fd) {
        if (fd == -1) io_error("open");
    }
    ~File() { ::close(fd_); }
    File(const File&) = delete;
    File& operator=(const File&) = delete;
    int get() const { return fd_; }

private:
    int fd_;
};

class Mapping {
public:
    Mapping(int fd, std::size_t size) : size_(size) {
        if (!size) return;
        data_ = ::mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (data_ == MAP_FAILED) io_error("map table");
    }
    ~Mapping() { if (size_) ::munmap(data_, size_); }
    Mapping(const Mapping&) = delete;
    Mapping& operator=(const Mapping&) = delete;
    std::string_view bytes() const {
        return size_ ? std::string_view(static_cast<const char*>(data_), size_)
                     : std::string_view();
    }

private:
    void* data_ = nullptr;
    std::size_t size_;
};

std::size_t file_size(int fd) {
    struct stat status {};
    if (::fstat(fd, &status) == -1) io_error("stat table");
    if (!S_ISREG(status.st_mode)) throw DbError("58030", "table must be a regular file");
    if (status.st_size < 0 ||
        static_cast<std::uintmax_t>(status.st_size) > std::numeric_limits<std::size_t>::max())
        throw DbError("54000", "table too large for this address space");
    return static_cast<std::size_t>(status.st_size);
}

void sync_file(int fd) {
    while (::fsync(fd) == -1) {
        if (errno != EINTR) io_error("fsync");
    }
}

void truncate_to(int fd, std::size_t bytes) {
    while (::ftruncate(fd, static_cast<off_t>(bytes)) == -1) {
        if (errno != EINTR) io_error("truncate table");
    }
    sync_file(fd);
}

void write_at(int fd, std::size_t offset, std::string_view bytes) {
    while (!bytes.empty()) {
        auto written = ::pwrite(fd, bytes.data(), bytes.size(), static_cast<off_t>(offset));
        if (written == -1 && errno == EINTR) continue;
        if (written == -1) io_error("write table");
        if (!written) throw DbError("58030", "no progress writing table");
        offset += static_cast<std::size_t>(written);
        bytes.remove_prefix(static_cast<std::size_t>(written));
    }
}

void read_at(int fd, std::size_t offset, char* dst, std::size_t count) {
    while (count) {
        auto n = ::pread(fd, dst, count, static_cast<off_t>(offset));
        if (n == -1 && errno == EINTR) continue;
        if (n == -1) io_error("read table");
        if (!n) corrupt(offset);
        dst += n;
        offset += static_cast<std::size_t>(n);
        count -= static_cast<std::size_t>(n);
    }
}

std::uint32_t checksum(std::string_view bytes) {
    // FNV-1a detects accidental damage. It is not an authentication code.
    std::uint32_t hash = 2166136261u;
    for (unsigned char byte : bytes) {
        hash ^= byte;
        hash *= 16777619u;
    }
    return hash;
}

std::uint32_t u32(const char* p) {
    auto b = reinterpret_cast<const unsigned char*>(p);
    return std::uint32_t(b[0]) | (std::uint32_t(b[1]) << 8) |
           (std::uint32_t(b[2]) << 16) | (std::uint32_t(b[3]) << 24);
}

std::uint64_t u64(const char* p) {
    return u32(p) | (std::uint64_t(u32(p + 4)) << 32);
}

void put32(char* p, std::uint32_t n) {
    for (int i = 0; i < 4; ++i) p[i] = static_cast<char>(n >> (8 * i));
}

struct DiskRow {
    std::uint64_t id;
    std::string_view first;
    std::string_view second;
};

std::size_t text_size(const char* row) {
    return static_cast<std::size_t>(u32(row + 8)) + u32(row + 12);
}

DiskRow unpack_row(std::string_view bytes, std::size_t offset) {
    if (bytes.size() < row_header_size || text_size(bytes.data()) > bytes.size() - row_header_size)
        corrupt(offset);
    auto first_size = u32(bytes.data() + 8);
    return {u64(bytes.data()), bytes.substr(row_header_size, first_size),
            bytes.substr(row_header_size + first_size, u32(bytes.data() + 12))};
}

// A short final frame is an interrupted INSERT. Invalid complete frames, even at
// EOF, are errors. The returned boundary is safe to truncate after unmapping.
template<class Visitor>
std::size_t scan(std::string_view file, bool verify, Visitor visit) {
    std::size_t start = 0;
    while (start < file.size()) {
        auto tail = file.substr(start);
        auto prefix = std::min(tail.size(), frame_magic.size());
        if (tail.substr(0, prefix) != frame_magic.substr(0, prefix)) corrupt(start);
        if (tail.size() < header_size) break;
        if (u32(tail.data() + 16) != checksum(tail.substr(0, 16))) corrupt(start);
        std::size_t payload_size = u32(tail.data() + 4);
        auto count = u32(tail.data() + 8);
        if (!count || payload_size > max_payload || count > payload_size / row_header_size)
            corrupt(start);
        std::size_t total = header_size + payload_size + commit_marker.size();
        if (tail.size() < total) break;
        auto payload = tail.substr(header_size, payload_size);
        if (tail.substr(header_size + payload_size, commit_marker.size()) != commit_marker ||
            (verify && checksum(payload) != u32(tail.data() + 12))) corrupt(start);
        std::size_t pos = 0;
        for (std::uint32_t i = 0; i < count; ++i) {
            auto offset = start + header_size + pos;
            auto row = unpack_row(payload.substr(pos), offset);
            visit(row, offset);
            pos += row_header_size + row.first.size() + row.second.size();
        }
        if (pos != payload.size()) corrupt(start);
        start += total;
    }
    return start;
}

enum class Column { Id, First, Second };

struct Schema {
    const char* table;
    const char* first;
    const char* second;

    Column column_of(const std::string& name) const {
        if (name == "id") return Column::Id;
        if (name == first) return Column::First;
        if (name == second) return Column::Second;
        throw DbError("42703", "unknown column: " + name);
    }
};

std::int64_t parse_id(std::string_view value) {
    if (!value.empty() && value.front() == '+') {
        value.remove_prefix(1);
        if (value.empty() || value.front() == '-') throw DbError("22P02", "invalid BIGINT id");
    }
    std::int64_t id = 0;
    auto parsed = std::from_chars(value.data(), value.data() + value.size(), id);
    if (parsed.ec == std::errc::result_out_of_range) throw DbError("22003", "id outside BIGINT range");
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size())
        throw DbError("22P02", "invalid BIGINT id");
    return id;
}

Row project(const std::vector<Column>& columns, const DiskRow& row) {
    Row output;
    output.reserve(columns.size());
    std::size_t bytes = 0;
    std::string id;
    for (auto col : columns) {
        std::string_view value;
        switch (col) {
            case Column::Id:
                if (id.empty()) id = std::to_string(row.id);
                value = id;
                break;
            case Column::First: value = row.first; break;
            case Column::Second: value = row.second; break;
        }
        if (value.size() > max_payload - bytes)
            throw DbError("54000", "projected row exceeds 64 MiB");
        bytes += value.size();
        output.emplace_back(value);
    }
    return output;
}

struct Table {
    Schema schema;
    File table;
    std::shared_mutex mutex;
    std::unique_ptr<BPlusTree> index;
    std::size_t committed = 0;
    std::uint64_t last_id = 0;
    bool available = true;

    Table(const std::filesystem::path& directory, Schema definition)
        : schema(definition),
          table(::open((directory / (std::string(schema.table) + ".tbl")).c_str(),
                       O_RDWR | O_CREAT | O_CLOEXEC, 0600)) {
        if (::flock(table.get(), LOCK_EX | LOCK_NB) == -1) {
            if (errno == EWOULDBLOCK) throw DbError("55006", "database directory is already in use");
            io_error("lock table");
        }
        rebuild();
    }

    void rebuild() {
        auto next = std::make_unique<BPlusTree>();
        std::uint64_t latest = 0;
        auto length = file_size(table.get());
        std::size_t end;
        {
            Mapping map(table.get(), length);
            end = scan(map.bytes(), true, [&](const DiskRow& row, std::size_t offset) {
                if (row.id != latest + 1 || row.id > max_id) corrupt(offset);
                next->insert(row.id, offset);
                latest = row.id;
            });
        }
        if (end != length) truncate_to(table.get(), end);
        index = std::move(next);
        last_id = latest;
        committed = end;
    }

    void check_available() const {
        if (!available) throw DbError("58030", "table requires reopening after a failed write");
    }

    Result select(const Statement& stmt, const ResultSink* sink) {
        Result result;
        if (stmt.columns.size() > 32767) throw DbError("54000", "too many output columns");
        result.columns = stmt.columns.empty() ? std::vector<std::string>{"id", schema.first, schema.second}
                                              : stmt.columns;
        std::vector<Column> projection;
        for (const auto& name : result.columns) projection.push_back(schema.column_of(name));

        std::optional<Column> filter;
        if (stmt.where) filter = schema.column_of(stmt.where->column);
        const bool by_id = filter == Column::Id;
        auto id = by_id ? parse_id(stmt.where->value) : 0;
        result.access = by_id ? AccessPath::IndexLookup : AccessPath::SequentialScan;

        // Committed bytes are immutable. Copy the EOF and any matching offset
        // together; later appends cannot affect this statement's snapshot.
        std::size_t snapshot_end;
        std::optional<std::uint64_t> offset;
        {
            std::shared_lock lock(mutex);
            check_available();
            snapshot_end = committed;
            if (by_id && id > 0) offset = index->find(static_cast<std::uint64_t>(id));
        }

        if (sink) sink->columns(result.columns);
        std::uint64_t returned = 0;
        auto emit = [&](const DiskRow& row) {
            auto output = project(projection, row);
            if (sink) sink->row(output);
            else result.rows.push_back(std::move(output));
            ++returned;
        };
        if (offset) {
            char head[row_header_size];
            if (*offset > snapshot_end || snapshot_end - *offset < row_header_size) corrupt(*offset);
            read_at(table.get(), *offset, head, sizeof(head));
            auto size = text_size(head);
            if (u64(head) != static_cast<std::uint64_t>(id) || size > max_payload ||
                size > snapshot_end - *offset - row_header_size) corrupt(*offset);
            std::string record(row_header_size + size, '\0');
            std::memcpy(record.data(), head, sizeof(head));
            read_at(table.get(), *offset + row_header_size, record.data() + row_header_size, size);
            emit(unpack_row(record, *offset));
            ++result.rows_examined;
        } else if (!by_id) {
            Mapping map(table.get(), snapshot_end);
            auto end = scan(map.bytes(), false, [&](const DiskRow& row, std::size_t) {
                ++result.rows_examined;
                if (filter && (filter == Column::First ? row.first : row.second) != stmt.where->value)
                    return;
                emit(row);
            });
            if (end != snapshot_end) corrupt(end);
        }
        result.tag = "SELECT " + std::to_string(returned);
        return result;
    }

    Result insert(const Statement& stmt) {
        std::size_t first_at = 0, second_at = 1;
        if (!stmt.columns.empty()) {
            bool first_seen = false, second_seen = false;
            for (std::size_t i = 0; i < stmt.columns.size(); ++i) {
                auto column = schema.column_of(stmt.columns[i]);
                if (column == Column::Id) throw DbError("428C9", "id is generated automatically");
                bool& seen = column == Column::First ? first_seen : second_seen;
                if (seen) throw DbError("42701", "duplicate target column: " + stmt.columns[i]);
                seen = true;
                (column == Column::First ? first_at : second_at) = i;
            }
            if (!first_seen || !second_seen)
                throw DbError("23502", std::string(schema.first) + " and " + schema.second + " are both required");
        }
        if (stmt.values.empty()) throw DbError("42601", "INSERT needs at least one row");
        std::size_t payload_size = 0;
        for (const auto& row : stmt.values) {
            if (row.size() != 2) throw DbError("42601", "expected two values per row");
            if (row[0].size() > max_payload || row[1].size() > max_payload ||
                row_header_size + row[0].size() + row[1].size() > max_payload - payload_size)
                throw DbError("54000", "INSERT payload exceeds 64 MiB");
            payload_size += row_header_size + row[0].size() + row[1].size();
        }
        std::string frame(header_size + payload_size + commit_marker.size(), '\0');
        Result result{{}, {}, "INSERT 0 " + std::to_string(stmt.values.size())};
        std::unique_lock lock(mutex);
        check_available();
        if (stmt.values.size() > max_id - last_id) throw DbError("22003", "id space exhausted");
        if (committed > static_cast<std::uintmax_t>(std::numeric_limits<off_t>::max()) - frame.size())
            throw DbError("54000", "table size limit exceeded");

        auto id = last_id;
        std::size_t pos = header_size;
        std::vector<std::size_t> offsets;
        offsets.reserve(stmt.values.size());
        for (const auto& values : stmt.values) {
            ++id;
            offsets.push_back(committed + pos);
            const auto& first = values[first_at];
            const auto& second = values[second_at];
            char* head = frame.data() + pos;
            put32(head, static_cast<std::uint32_t>(id));
            put32(head + 4, static_cast<std::uint32_t>(id >> 32));
            put32(head + 8, static_cast<std::uint32_t>(first.size()));
            put32(head + 12, static_cast<std::uint32_t>(second.size()));
            std::memcpy(head + row_header_size, first.data(), first.size());
            std::memcpy(head + row_header_size + first.size(), second.data(), second.size());
            pos += row_header_size + first.size() + second.size();
        }
        std::memcpy(frame.data(), frame_magic.data(), frame_magic.size());
        put32(frame.data() + 4, static_cast<std::uint32_t>(payload_size));
        put32(frame.data() + 8, static_cast<std::uint32_t>(offsets.size()));
        put32(frame.data() + 12, checksum(std::string_view(frame).substr(header_size, payload_size)));
        put32(frame.data() + 16, checksum(std::string_view(frame).substr(0, 16)));
        std::memcpy(frame.data() + pos, commit_marker.data(), commit_marker.size());

        bool touched_index = false;
        try {
            write_at(table.get(), committed, frame);
            sync_file(table.get());
            touched_index = true;
            auto key = last_id;
            for (auto offset : offsets) index->insert(++key, offset);
        } catch (...) {
            // Never expose half of a failed write. If the rollback itself fails,
            // all subsequent requests must wait for a fresh recovery.
            try {
                truncate_to(table.get(), committed);
                if (touched_index) rebuild();
            } catch (...) {
                available = false;
                throw;
            }
            throw;
        }
        committed += frame.size();
        last_id = id;
        return result;
    }
};

} // namespace

struct Engine::Impl {
    // Always acquire the file locks in this order, before exposing the engine.
    std::array<Table, 2> tables;

    explicit Impl(const std::filesystem::path& dir)
        : tables{{{dir, {"users", "name", "email"}}, {dir, {"products", "name", "sku"}}}} {
        File directory(::open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC));
        sync_file(directory.get());
    }
};

Engine::Engine(std::filesystem::path directory) {
    std::error_code error;
    std::filesystem::create_directories(directory, error);
    if (error) throw DbError("58030", "create database directory: " + error.message());
    impl_ = std::make_unique<Impl>(directory);
}

Engine::~Engine() = default;

Result Engine::execute(const Statement& statement, const ResultSink* sink) {
    for (auto& table : impl_->tables) {
        if (statement.table == table.schema.table)
            return statement.kind == Statement::Kind::Insert ? table.insert(statement) : table.select(statement, sink);
    }
    throw DbError("42P01", "unknown table: " + statement.table);
}

} // namespace datom
