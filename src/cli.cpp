#include "engine.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>

namespace {

const char* usage = "Usage: datom [--data DIRECTORY] [-f FILE | -c SQL]\n"
                    "Read a SQL script from standard input when neither option is given.\n";

std::string read_script(std::istream& input) {
    std::string contents;
    char buffer[16384];
    do {
        input.read(buffer, sizeof(buffer));
        contents.append(buffer, static_cast<std::size_t>(input.gcount()));
    } while (input);
    if (input.bad() || !input.eof()) throw std::runtime_error("could not read SQL input");
    return contents;
}

void print_row(const datom::Row& row) {
    for (std::size_t i = 0; i < row.size(); ++i) {
        if (i != 0) std::cout.put('\t');
        for (char c : row[i]) {
            switch (c) {
                case '\\': std::cout << "\\\\"; break;
                case '\n': std::cout << "\\n"; break;
                case '\r': std::cout << "\\r"; break;
                case '\t': std::cout << "\\t"; break;
                default: std::cout.put(c);
            }
        }
    }
    std::cout.put('\n');
}

} // namespace

int main(int argc, char** argv) {
    try {
        std::filesystem::path data = "data";
        std::optional<std::string> filename;
        std::optional<std::string> command;
        for (int i = 1; i < argc; ++i) {
            const std::string arg = argv[i];
            if (arg == "--help" || arg == "-h") { std::cout << usage; return 0; }
            if (arg != "--data" && arg != "-f" && arg != "-c")
                throw std::runtime_error("unknown option: " + arg);
            if (++i == argc) throw std::runtime_error("missing value for " + arg);
            if (arg == "--data") data = argv[i];
            else {
                if (filename || command) throw std::runtime_error("choose one SQL input");
                if (arg == "-f") filename = argv[i];
                else command = argv[i];
            }
        }

        std::string script;
        if (command) script = std::move(*command);
        else if (filename) {
            if (std::filesystem::is_directory(*filename))
                throw std::runtime_error("SQL input is a directory: " + *filename);
            std::ifstream input(*filename, std::ios::binary);
            if (!input) throw std::runtime_error("could not open SQL file: " + *filename);
            script = read_script(input);
        } else script = read_script(std::cin);

        datom::Engine engine(data);
        const datom::ResultSink output{print_row, print_row};
        datom::for_each_statement(script, [&](datom::Statement&& statement) {
            auto result = engine.execute(statement, &output);
            std::cout << result.tag << '\n';
        });
        std::cout.flush();
        return std::cout ? 0 : 1;
    } catch (const datom::DbError& e) {
        std::cerr << "ERROR [" << e.code << "] " << e.what() << '\n';
    } catch (const std::exception& e) {
        std::cerr << "ERROR " << e.what() << '\n';
    }
    return 1;
}
