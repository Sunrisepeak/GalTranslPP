// Engine comparison (temporary): one body, five ways to reach the same libraries.
// Modules only: import std and the project's wrapper modules of the same three libraries.

import std;
import AbslContainers;
import nlohmann.json;
import spdlog;

namespace bench_m
{
    int run(const std::vector<std::string>& names)
    {
        std::vector<int> sizes;
        for (const auto& name : names)
            sizes.push_back(static_cast<int>(name.size()));
        std::ranges::sort(sizes);
        const auto longNames = std::ranges::count_if(sizes, [](int n) { return n > 3; });
        nlohmann::json doc = nlohmann::json::object();
        doc["count"] = longNames;
        doc["names"] = names;
        absl::flat_hash_map<std::string, int> index;
        for (const auto& name : names)
            index[name] = static_cast<int>(name.size());
        spdlog::info("{} names, {} long", names.size(), longNames);
        const std::string text = doc.dump();
        return static_cast<int>(text.size() + index.size());
    }
}
