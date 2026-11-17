#pragma once

#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace datom {

using Row = std::vector<std::string>;

struct Predicate {
    std::string column;
    std::string value;
};

struct Statement {
    enum class Kind { Insert, Select };
    Kind kind;
    std::string table;
    // Empty for SELECT * and INSERT with an implicit column list.
    std::vector<std::string> columns;
    std::vector<Row> values;
    std::optional<Predicate> where;
};

class DbError : public std::runtime_error {
public:
    std::string code;
    DbError(std::string code, std::string message);
};

std::vector<Statement> parse_sql(std::string_view sql);
void for_each_statement(std::string_view sql, const std::function<void(Statement&&)>& visit);

} // namespace datom
