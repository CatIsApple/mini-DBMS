#include "engine.hpp"
#include "pgwire.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <netdb.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <system_error>
#include <sys/socket.h>
#include <sys/time.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;
constexpr auto idle_timeout = std::chrono::seconds(60);
constexpr int io_timeout_seconds = 10;
volatile std::sig_atomic_t stopping = 0;
volatile std::sig_atomic_t signal_pipe = -1;

void wake_loop(int fd) {
    const char byte = 1;
    while (::write(fd, &byte, 1) < 0 && errno == EINTR) {}
    // A full nonblocking pipe already contains a wakeup.
}

void stop_handler(int) {
    int saved = errno;
    stopping = 1;
    if (signal_pipe >= 0) wake_loop(signal_pipe);
    errno = saved;
}

[[noreturn]] void fail(const char* what) {
    throw std::system_error(errno, std::generic_category(), what);
}

class Fd {
public:
    explicit Fd(int fd) : fd_(fd) {}
    ~Fd() { if (fd_ >= 0) ::close(fd_); }
    Fd(Fd&& other) noexcept : fd_(other.fd_) { other.fd_ = -1; }
    Fd(const Fd&) = delete;
    int get() const { return fd_; }
private:
    int fd_;
};

void set_blocking(int fd, bool blocking) {
    int flags = ::fcntl(fd, F_GETFL);
    if (flags < 0 || ::fcntl(fd, F_SETFL, blocking ? flags & ~O_NONBLOCK : flags | O_NONBLOCK) < 0)
        fail("fcntl");
}

struct Wakeup {
    int ends[2];
    Wakeup() {
        if (::pipe(ends) < 0) fail("pipe");
        try {
            for (int fd : ends) set_blocking(fd, false);
        } catch (...) {
            ::close(ends[0]);
            ::close(ends[1]);
            throw;
        }
        signal_pipe = ends[1];
    }
    ~Wakeup() {
        signal_pipe = -1;
        ::close(ends[0]);
        ::close(ends[1]);
    }
    void drain() const {
        char bytes[512];
        while (::read(ends[0], bytes, sizeof(bytes)) > 0) {}
    }
};

struct Options {
    std::string host = "127.0.0.1", port = "5433", data = "./data";
    unsigned workers = 4, queue = 64;
};

unsigned number(const std::string& arg, unsigned min, unsigned max) {
    unsigned value = 0;
    auto parsed = std::from_chars(arg.data(), arg.data() + arg.size(), value);
    if (parsed.ec != std::errc() || parsed.ptr != arg.data() + arg.size() || value < min || value > max)
        throw std::runtime_error("invalid numeric option: " + arg);
    return value;
}

Options options(int argc, char** argv) {
    Options out;
    for (int i = 1; i < argc; ++i) {
        std::string key = argv[i];
        if (key == "--help" || key == "-h") {
            std::cout << "Usage: " << argv[0]
                      << " [--host ADDRESS] [--port PORT] [--data DIRECTORY] [--workers N] [--queue N]\n"
                      << "Defaults: 127.0.0.1, 5433, ./data, 4 workers, 64 queued messages\n";
            std::exit(0);
        }
        if (++i == argc) throw std::runtime_error("missing value for " + key);
        std::string value = argv[i];
        if (key == "--host") out.host = value;
        else if (key == "--port") out.port = std::to_string(number(value, 0, 65535));
        else if (key == "--data") out.data = value;
        else if (key == "--workers") out.workers = number(value, 1, 256);
        else if (key == "--queue") out.queue = number(value, 1, 65535);
        else throw std::runtime_error("unknown option: " + key);
    }
    return out;
}

Fd listen_on(const Options& opt) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_NUMERICSERV;
    addrinfo* list = nullptr;
    int error = ::getaddrinfo(opt.host.c_str(), opt.port.c_str(), &hints, &list);
    if (error) throw std::runtime_error(gai_strerror(error));
    std::unique_ptr<addrinfo, decltype(&freeaddrinfo)> addresses(list, freeaddrinfo);
    int last_error = EADDRNOTAVAIL;
    for (auto* addr = list; addr; addr = addr->ai_next) {
        Fd fd(::socket(addr->ai_family, addr->ai_socktype, addr->ai_protocol));
        int yes = 1;
        if (fd.get() >= 0 &&
            ::setsockopt(fd.get(), SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) == 0 &&
            ::bind(fd.get(), addr->ai_addr, addr->ai_addrlen) == 0 &&
            ::listen(fd.get(), SOMAXCONN) == 0) {
            set_blocking(fd.get(), false);
            return fd;
        }
        last_error = errno;
    }
    throw std::system_error(last_error, std::generic_category(), "TCP listen");
}

std::string endpoint(int fd) {
    sockaddr_storage addr{};
    socklen_t len = sizeof(addr);
    if (::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len) < 0) fail("getsockname");
    char host[NI_MAXHOST], port[NI_MAXSERV];
    int err = ::getnameinfo(reinterpret_cast<sockaddr*>(&addr), len, host, sizeof(host),
                            port, sizeof(port), NI_NUMERICHOST | NI_NUMERICSERV);
    if (err) throw std::runtime_error(gai_strerror(err));
    return addr.ss_family == AF_INET6 ? "[" + std::string(host) + "]:" + port
                                      : std::string(host) + ":" + port;
}

enum class State { idle, busy, finished, closed };

struct Client {
    explicit Client(Fd socket) : fd(std::move(socket)) {}
    Fd fd;
    datom::SessionPhase phase = datom::SessionPhase::Startup;
    Clock::time_point last_activity = Clock::now();
    std::atomic<State> state{State::idle};
};

class Workers {
public:
    Workers(datom::Engine& engine, int wake_fd, const Options& opt)
        : engine_(engine), wake_fd_(wake_fd), capacity_(opt.queue) {
        try {
            for (unsigned i = 0; i < opt.workers; ++i)
                threads_.emplace_back([this] { run(); });
        } catch (...) {
            stop();
            throw;
        }
    }
    ~Workers() { stop(); }

    bool has_room() {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.size() < capacity_;
    }
    bool submit(Client& client) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.size() == capacity_) return false;
        queue_.push_back(&client);
        client.state = State::busy;
        ready_.notify_one();
        return true;
    }
private:
    void stop() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            done_ = true;
            queue_.clear();
        }
        ready_.notify_all();
        for (auto& t : threads_) t.join();
    }
    void run() {
        for (;;) {
            Client* client;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [&] { return done_ || !queue_.empty(); });
                if (done_) return;
                client = queue_.front();
                queue_.pop_front();
            }
            bool keep = false;
            try {
                keep = datom::serve_message(client->fd.get(), engine_, client->phase);
            } catch (const std::exception&) {
                // A socket may close or time out halfway through a message.
            }
            // The reactor may destroy the connection immediately after this store.
            client->state = keep ? State::finished : State::closed;
            wake_loop(wake_fd_);
        }
    }
    datom::Engine& engine_;
    int wake_fd_;
    std::size_t capacity_;
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<Client*> queue_;
    std::vector<std::thread> threads_;
    bool done_ = false;
};

using Clients = std::map<int, Client>;

bool accept_clients(int listener, Clients& clients, std::size_t limit) {
    // Limit one batch so a flood of new connections cannot monopolize the reactor.
    for (int i = 0; i < 64; ++i) {
        Fd fd(::accept(listener, nullptr, nullptr));
        if (fd.get() < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) return true;
            if (errno == EINTR || errno == ECONNABORTED) continue;
            if (errno == EMFILE || errno == ENFILE || errno == ENOBUFS || errno == ENOMEM)
                return false;
            fail("accept");
        }
        if (clients.size() >= limit) continue;
        set_blocking(fd.get(), true);
        timeval timeout{io_timeout_seconds, 0};
        int yes = 1;
        if (::setsockopt(fd.get(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
            ::setsockopt(fd.get(), SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0 ||
            ::setsockopt(fd.get(), IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes)) < 0)
            continue;
        int key = fd.get();
        clients.try_emplace(key, std::move(fd));
    }
    return true;
}

void serve(const Options& opt) {
    Wakeup wake;
    struct sigaction action{};
    sigemptyset(&action.sa_mask);
    action.sa_handler = stop_handler;
    if (sigaction(SIGTERM, &action, nullptr) || sigaction(SIGINT, &action, nullptr)) fail("sigaction");
    action.sa_handler = SIG_IGN;
    if (sigaction(SIGPIPE, &action, nullptr)) fail("sigaction");

    datom::Engine engine(opt.data);
    Fd listener = listen_on(opt);
    Clients clients;
    Workers workers(engine, wake.ends[1], opt);
    const std::size_t limit = std::max(256U, opt.workers + opt.queue);
    std::cout << "listening on " << endpoint(listener.get()) << std::endl;
    int last_served = -1;
    Clock::time_point accept_after{};
    try {
        while (!stopping) {
            auto now = Clock::now();
            std::vector<pollfd> fds{{wake.ends[0], POLLIN, 0},
                                    {now >= accept_after ? listener.get() : -1, POLLIN, 0}};
            bool have_room = workers.has_room();
            for (auto it = clients.begin(); it != clients.end();) {
                auto& client = it->second;
                auto state = client.state.load();
                if (state == State::finished) {
                    client.last_activity = now;
                    client.state = state = State::idle;
                }
                if (state == State::closed ||
                    (state == State::idle && now - client.last_activity >= idle_timeout)) {
                    it = clients.erase(it);
                    continue;
                }
                if (have_room && state == State::idle) fds.push_back({it->first, POLLIN, 0});
                ++it;
            }
            // Rotate the ready scan so a pipelining client cannot starve newer connections.
            auto first = fds.begin() + 2;
            auto next = std::upper_bound(first, fds.end(), last_served,
                                         [](int fd, const pollfd& p) { return fd < p.fd; });
            std::rotate(first, next, fds.end());
            if (::poll(fds.data(), fds.size(), 1000) < 0) {
                if (errno == EINTR) continue;
                fail("poll");
            }
            if (stopping) break;
            if (fds[0].revents) wake.drain();
            if ((fds[1].revents & POLLIN) && !accept_clients(listener.get(), clients, limit))
                accept_after = Clock::now() + std::chrono::seconds(1);
            for (std::size_t i = 2; i < fds.size(); ++i) {
                if (!fds[i].revents) continue;
                if (!workers.submit(clients.at(fds[i].fd))) break;
                last_served = fds[i].fd;
            }
        }
    } catch (...) {
        for (auto& entry : clients) ::shutdown(entry.first, SHUT_RDWR);
        throw;
    }
    // Wake workers blocked on partial frames or a client that stopped reading.
    for (auto& entry : clients) ::shutdown(entry.first, SHUT_RDWR);
}

} // namespace

int main(int argc, char** argv) {
    try {
        serve(options(argc, argv));
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
