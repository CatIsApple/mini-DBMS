CXX ?= c++
CPPFLAGS += -Isrc
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++17 -Wall -Wextra -Wpedantic -pthread
LDLIBS += -pthread

CORE := build/sql.o build/engine.o
all: build/datom

build:
	mkdir -p $@

build/%.o: src/%.cpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/datom: build/cli.o $(CORE)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/sql-test: tests/sql_test.cpp build/sql.o
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/engine-test: tests/engine_test.cpp $(CORE)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

test: all build/sql-test build/engine-test
	./build/sql-test
	./build/engine-test

clean:
	rm -rf build

.PHONY: all test clean
-include $(wildcard build/*.d)
