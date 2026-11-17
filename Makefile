CXX ?= c++
CPPFLAGS += -Isrc
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++17 -Wall -Wextra -Wpedantic -pthread
LDLIBS += -pthread
PSQL ?= psql

CORE := build/sql.o build/engine.o
all: build/datom build/datom-server

build:
	mkdir -p $@

build/%.o: src/%.cpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/datom: build/cli.o $(CORE)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/datom-server: build/server.o build/pgwire.o $(CORE)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/sql-test: tests/sql_test.cpp build/sql.o
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/bptree-test: tests/bptree_test.cpp src/bptree.hpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $< $(LDLIBS) -o $@

build/engine-test: tests/engine_test.cpp $(CORE)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/datom-bench: bench/benchmark.cpp $(CORE)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

test: all build/sql-test build/bptree-test build/engine-test
	./build/sql-test
	./build/bptree-test
	./build/engine-test
	python3 tests/integration.py --cli ./build/datom --server ./build/datom-server

bench: build/datom-bench
	./build/datom-bench

test-psql: build/datom-server
	python3 tests/psql_smoke.py --server ./build/datom-server --psql "$(PSQL)"

clean:
	rm -rf build

.PHONY: all test test-psql bench clean
-include $(wildcard build/*.d)
