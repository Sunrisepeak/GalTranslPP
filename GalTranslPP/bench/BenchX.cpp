// Engine comparison (temporary): one body, five ways to reach the same libraries.
// Mixed as NormalJsonTranslator.Run.cpp is: the platform headers by #include, the rest by import.

#define PYBIND11_HEADERS
#define LUABRIDGE3_HEADERS
#include "GPPMacros.hpp"
#ifdef _WIN32
#include <Windows.h>
#include <Shlwapi.h>
#endif
#include <toml.hpp>
#include <proxy/proxy.h>
import std;
import AbslContainers;
import nlohmann.json;
import spdlog;

namespace bench_x
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
