#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

namespace datom {

// Internal separators are the smallest key in their right subtree. File
// offsets live exclusively in the leaves. The caller synchronizes access.
class BPlusTree {
public:
    BPlusTree() : root_(std::make_unique<Node>(true)) {}

    std::optional<std::uint64_t> find(std::uint64_t key) const {
        const Node* node = root_.get();
        while (!node->leaf) {
            node = node->children[child_for(*node, key)].get();
        }
        const auto it = std::lower_bound(node->keys.begin(), node->keys.end(), key);
        if (it == node->keys.end() || *it != key) {
            return std::nullopt;
        }
        return node->offsets[it - node->keys.begin()];
    }

    // Insert a new key, or replace the file offset for an existing key.
    void insert(std::uint64_t key, std::uint64_t offset) {
        if (root_->full()) {
            auto parent = std::make_unique<Node>(false);
            auto split = split_node(*root_);
            parent->keys.push_back(split.separator);
            parent->children.push_back(std::move(root_));
            parent->children.push_back(std::move(split.right));
            root_ = std::move(parent);
        }

        Node* node = root_.get();
        while (!node->leaf) {
            auto child = child_for(*node, key);
            // Splitting before descent means the parent always has space.
            if (node->children[child]->full()) {
                auto split = split_node(*node->children[child]);
                node->keys.insert(node->keys.begin() + child, split.separator);
                node->children.insert(node->children.begin() + child + 1,
                                      std::move(split.right));
                if (key >= split.separator) {
                    ++child;
                }
            }
            node = node->children[child].get();
        }

        const auto it = std::lower_bound(node->keys.begin(), node->keys.end(), key);
        const auto slot = it - node->keys.begin();
        if (it != node->keys.end() && *it == key) {
            node->offsets[slot] = offset;
            return;
        }
        node->keys.insert(it, key);
        node->offsets.insert(node->offsets.begin() + slot, offset);
    }

private:
    static constexpr std::size_t capacity = 64;

    struct Node {
        explicit Node(bool is_leaf) : leaf(is_leaf) {
            keys.reserve(leaf ? capacity : capacity - 1);
            if (leaf) {
                offsets.reserve(capacity);
            } else {
                children.reserve(capacity);
            }
        }

        bool full() const {
            return keys.size() == (leaf ? capacity : capacity - 1);
        }

        bool leaf;
        std::vector<std::uint64_t> keys;
        std::vector<std::uint64_t> offsets;
        std::vector<std::unique_ptr<Node>> children;
        Node* next_leaf = nullptr;
    };

    struct Split {
        std::uint64_t separator;
        std::unique_ptr<Node> right;
    };

    static std::size_t child_for(const Node& node, std::uint64_t key) {
        return static_cast<std::size_t>(
            std::upper_bound(node.keys.begin(), node.keys.end(), key) - node.keys.begin());
    }

    static Split split_node(Node& left) {
        auto right = std::make_unique<Node>(left.leaf);
        const auto middle = left.keys.size() / 2;
        const auto separator = left.keys[middle];
        // A leaf retains the separator as a data key. An internal node
        // promotes it to the parent and divides its children around it.
        const auto first_right = left.leaf ? middle : middle + 1;
        right->keys.assign(left.keys.begin() + first_right, left.keys.end());
        left.keys.resize(middle);
        if (left.leaf) {
            right->offsets.assign(left.offsets.begin() + middle, left.offsets.end());
            left.offsets.resize(middle);
            right->next_leaf = left.next_leaf;
            left.next_leaf = right.get();
        } else {
            right->children.assign(std::make_move_iterator(left.children.begin() + first_right),
                                   std::make_move_iterator(left.children.end()));
            left.children.resize(first_right);
        }
        return {separator, std::move(right)};
    }

    std::unique_ptr<Node> root_;
};

} // namespace datom
