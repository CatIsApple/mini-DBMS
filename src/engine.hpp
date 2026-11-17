#pragma once

#include "sql.hpp"

#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>

namespace datom {

enum class AccessPath { None, SequentialScan };

struct Result {
    std::vector<std::string> columns;
    std::vector<Row> rows;
    std::string tag;
    AccessPath access = AccessPath::None;
    std::uint64_t rows_examined = 0;
};

// SELECT calls columns once and row once per match. Callbacks run outside the
// table lock, and row references are valid for the duration of the callback.
struct ResultSink {
    std::function<void(const Row&)> columns;
    std::function<void(const Row&)> row;
};

class Engine {
public:
    explicit Engine(std::filesystem::path directory);
    ~Engine();
    Engine(const Engine&) = delete;
    Engine& operator=(const Engine&) = delete;

    Result execute(const Statement& statement, const ResultSink* sink = nullptr);

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace datom
