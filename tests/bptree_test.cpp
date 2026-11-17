#include "bptree.hpp"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using datom::BPlusTree;

void expect_offset(const BPlusTree& tree, std::uint64_t key, std::uint64_t offset) {
    if (tree.find(key) != offset) {
        throw std::runtime_error("unexpected B+ tree offset for key " + std::to_string(key));
    }
}

void expect_missing(const BPlusTree& tree, std::uint64_t key) {
    if (tree.find(key)) {
        throw std::runtime_error("unexpected B+ tree hit for key " + std::to_string(key));
    }
}

void boundaries_and_replacement() {
    BPlusTree tree;
    constexpr auto last = std::numeric_limits<std::uint64_t>::max();
    expect_missing(tree, 0);
    expect_missing(tree, last);
    tree.insert(0, last);
    tree.insert(last, 0);
    tree.insert(last - 1, last - 1);
    expect_offset(tree, 0, last);
    expect_offset(tree, last, 0);
    expect_offset(tree, last - 1, last - 1);
    tree.insert(last, 17);
    expect_offset(tree, last, 17);
    expect_missing(tree, 1);
}

void check_order(const std::vector<std::uint64_t>& order) {
    BPlusTree tree;
    for (const auto number : order) {
        const auto key = number * 2;
        tree.insert(key, key + 51);
        expect_offset(tree, key, key + 51);
    }
    // Revisiting every key from the root also checks separator boundaries.
    for (std::uint64_t number = 0; number < order.size(); ++number) {
        const auto key = number * 2;
        expect_offset(tree, key, key + 51);
        expect_missing(tree, key + 1);
    }
    for (std::uint64_t number = 0; number < order.size(); number += 37) {
        tree.insert(number * 2, number + 1);
    }
    for (std::uint64_t number = 0; number < order.size(); ++number) {
        expect_offset(tree, number * 2, number % 37 == 0 ? number + 1 : number * 2 + 51);
    }
}

} // namespace

int main() {
    boundaries_and_replacement();
    std::vector<std::uint64_t> order(150000);
    std::iota(order.begin(), order.end(), std::uint64_t{0});
    check_order(order);
    std::reverse(order.begin(), order.end());
    check_order(order);
    std::mt19937 rng(2026);
    std::shuffle(order.begin(), order.end(), rng);
    check_order(order);
    std::cout << "B+ tree tests passed\n";
}
