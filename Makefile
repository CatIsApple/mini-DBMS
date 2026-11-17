CXX ?= c++
CPPFLAGS += -Isrc
CXXFLAGS ?= -O2 -g
CXXFLAGS += -std=c++17 -Wall -Wextra -Wpedantic -pthread
LDLIBS += -pthread

all: build/sql-test

build:
	mkdir -p $@

build/%.o: src/%.cpp | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -MMD -MP -c $< -o $@

build/sql-test: tests/sql_test.cpp build/sql.o
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

test: all build/sql-test
	./build/sql-test

clean:
	rm -rf build

.PHONY: all test clean
-include $(wildcard build/*.d)
