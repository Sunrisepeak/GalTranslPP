#pragma once

// The project's parameters for the mcpp plugins every member uses: the vcpkg
// triplet, the translation settings, and the shared build-script files.
// Included only by build.mcpp programs, after `import mcpp.deps.vcpkg;` and
// `import mcpp.rules.qt;`.

// mcpp caches build.mcpp separately from its included headers. Track the shared
// build logic so editing a helper recompiles the programs that include it.
inline void track_build_scripts() {
    const auto directory = std::filesystem::path(mcpp::manifest_dir()).parent_path() /
                           "mcpp-build-scripts";
    for (const char* name : {"gpp_deps.hpp", "mcpp_actions.hpp"}) {
        const auto file = (directory / name).generic_string();
        mcpp::rerun_if_changed(file.c_str());
    }
}

inline constexpr const char* gpp_triplet = "gpp-x64-windows-release";

// Maps the workspace's vcpkg manifest into this member: the include directory
// always, the listed libraries when the member links them itself.
inline bool use_vcpkg(std::initializer_list<const char*> libraries = {}) {
    mcpp::deps::vcpkg::options options;
    options.triplet = gpp_triplet;
    for (const char* library : libraries) options.libraries.emplace_back(library);
    return static_cast<bool>(mcpp::deps::vcpkg::use(options));
}

// Qt's Visual Studio integration updates the TS file and releases the QM file
// before compiling each project; `update_sources` keeps that order.
inline mcpp::rules::qt::translations gpp_translations(const char* ts, const char* qm_dir = "") {
    mcpp::rules::qt::translations t;
    t.ts = {ts};
    t.update_sources = true;
    t.tr_function_alias = {"translate+=gppTr"};
    t.out_dir = qm_dir;
    return t;
}

// Where rules-qt writes a member's QM file when `qm_dir` is left empty.
inline std::filesystem::path gpp_qm(const char* stem) {
    return std::filesystem::path(mcpp::out_dir()) / "qt" / "translations" / (std::string(stem) + ".qm");
}
