// Engine comparison (temporary): one body, five ways to reach the same libraries.
// Headers only, with the platform headers NormalJsonTranslator.Run.cpp includes (pybind11, Lua, Windows, toml, proxy).

#define PYBIND11_HEADERS
#define LUABRIDGE3_HEADERS
#include "GPPMacros.hpp"
#ifdef _WIN32
#include <Windows.h>
#include <Shlwapi.h>
#endif
#include <toml.hpp>
#include <proxy/proxy.h>
#include <algorithm>
#include <string>
#include <vector>
#include <absl/container/flat_hash_map.h>
#include <nlohmann/json.hpp>
#include <spdlog/spdlog.h>

namespace bench_hx
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
