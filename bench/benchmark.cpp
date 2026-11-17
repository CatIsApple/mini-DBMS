#include "engine.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>

namespace {

using Clock = std::chrono::steady_clock;
using Microseconds = std::chrono::duration<double, std::micro>;
constexpr std::size_t records = 1'000'000;
constexpr std::size_t batch_size = 1'000;
constexpr std::size_t samples = 31;

struct TempDatabase {
    std::filesystem::path dir;
    TempDatabase() {
        std::string pattern = (std::filesystem::temp_directory_path() / "datom-bench-XXXXXX").string();
        if (!mkdtemp(pattern.data())) throw std::runtime_error("cannot create benchmark directory");
        dir = pattern;
    }
    ~TempDatabase() { std::error_code ec; std::filesystem::remove_all(dir, ec); }
};

datom::Result sql(datom::Engine& engine, const std::string& query) {
    return engine.execute(datom::parse_sql(query).at(0));
}

void insert_records(datom::Engine& engine) {
    for (std::size_t first = 1; first <= records; first += batch_size) {
        std::string query = "INSERT INTO users(name,email) VALUES ";
        for (std::size_t id = first; id < first + batch_size; ++id) {
            if (id != first) query.push_back(',');
            const auto suffix = std::to_string(id);
            query += "('user" + suffix + "','user" + suffix + "@example.org')";
        }
        if (sql(engine, query).tag != "INSERT 0 " + std::to_string(batch_size))
            throw std::runtime_error("unexpected inserted row count");
    }
}

double measure(datom::Engine& engine, std::size_t id, bool indexed) {
    const std::string key = std::to_string(id);
    const std::string name = "user" + key;
    const std::string query = "SELECT id, name FROM users WHERE " +
        (indexed ? "id = " + key : "name = '" + name + "'");
    const auto start = Clock::now();
    const auto result = sql(engine, query);
    const auto elapsed = Microseconds(Clock::now() - start).count();
    const auto path = indexed ? datom::AccessPath::IndexLookup : datom::AccessPath::SequentialScan;
    if (result.access != path || result.rows_examined != (indexed ? 1 : records)
            || result.rows.size() != 1 || result.rows.front() != datom::Row{key, name})
        throw std::runtime_error("unexpected access path, inspected rows, or result for " + query);
    return elapsed;
}

double percentile(std::vector<double> times, double fraction) {
    std::sort(times.begin(), times.end());
    return times.at(static_cast<std::size_t>(std::ceil(times.size() * fraction)) - 1);
}

} // namespace

int main() {
    try {
        TempDatabase temp;
        std::cout << std::fixed << std::setprecision(2);
        {
            datom::Engine engine(temp.dir);
            const auto start = Clock::now();
            insert_records(engine);
            std::cout << "inserted=" << records << " rows_per_statement=" << batch_size
                      << " insert_seconds=" << std::chrono::duration<double>(Clock::now() - start).count()
                      << std::endl;
        }
        const auto restart = Clock::now();
        datom::Engine reopened(temp.dir);
        std::cout << "reopen_seconds=" << std::chrono::duration<double>(Clock::now() - restart).count()
                  << std::endl;
        measure(reopened, records, false);
        measure(reopened, records, true);
        std::mt19937_64 random(20261117);
        std::uniform_int_distribution<std::size_t> pick(1, records);
        std::vector<double> indexed, scanned;
        for (std::size_t i = 0; i < samples; ++i) {
            const auto id = pick(random);
            if (i % 2 == 0) {
                indexed.push_back(measure(reopened, id, true));
                scanned.push_back(measure(reopened, id, false));
            } else {
                scanned.push_back(measure(reopened, id, false));
                indexed.push_back(measure(reopened, id, true));
            }
        }
        const auto index_median = percentile(indexed, 0.5);
        const auto scan_median = percentile(scanned, 0.5);
        std::cout << "queries_per_path=" << samples << '\n'
                  << "id_index median_us=" << index_median << " p95_us=" << percentile(indexed, 0.95)
                  << " rows_examined=1\n"
                  << "name_scan median_us=" << scan_median << " p95_us=" << percentile(scanned, 0.95)
                  << " rows_examined=" << records << '\n'
                  << "median_scan_to_index_ratio=" << scan_median / index_median << "x\n";
    } catch (const std::exception& error) {
        std::cerr << "benchmark failed: " << error.what() << '\n';
        return 1;
    }
}
