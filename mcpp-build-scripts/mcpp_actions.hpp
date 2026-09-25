#pragma once

// The release layout of the three executable workspace members: Release/GPPCLI,
// Release/GPPGUI, Release/GUICORE and the optional private mirrors. Included
// only by build.mcpp programs, after gpp_deps.hpp.

struct executable_actions {
    using path = std::filesystem::path;

    path project = path(mcpp::manifest_dir());
    path workspace = project.parent_path();
    path release = workspace / "Release";
    path qt = mcpp::rules::qt::root();
    path vcpkg = workspace / "vcpkg_installed" / gpp_triplet;
    // Further directories the runtime closure is read from: a CMake subproject's
    // installed `bin/` (GPPGUI adds ElaWidgetTools').
    std::vector<path> extra_runtime_dirs;
    std::string target;
    std::string target_file;
    unsigned next_action = 0;

    explicit executable_actions(std::string name)
        : target(std::move(name)), target_file("${mcpp.target_file:" + target + "}") {
        // Private release path files are optional, as in the VS post-build events.
        mcpp::rerun_if_changed(release.string().c_str());
    }

    path private_release_dir(std::string_view member) const {
        namespace fs = std::filesystem;
        const auto config = release /
            (member == "GPPCLI" ? "GPPCLI_PRIVATE.txt" : "GPPGUI_PRIVATE.txt");
        const auto config_name = config.string();
        mcpp::rerun_if_changed(config_name.c_str());
        std::error_code error;
        if (!fs::is_regular_file(config, error)) return {};

        std::ifstream input(config);
        std::string first_line;
        if (!std::getline(input, first_line)) return {};
        if (!first_line.empty() && first_line.back() == '\r') first_line.pop_back();
        if (first_line.empty()) return {};

        path destination(first_line);
        if (destination.is_relative()) destination = release / destination;
        destination = destination.lexically_normal();
        error.clear();
        if (!fs::is_directory(destination, error)) {
            std::println(stderr, "Private release directory is unavailable: {}", destination.string());
            return {};
        }
        return destination;
    }

    void copy(std::string source, const path& destination) {
        const auto output = destination.lexically_normal().string();
        const auto id = "stage-" + std::to_string(next_action++);
        mcpp::action action;
        action.id = id.c_str();
        action.role = "artifact";
        action.arg("${mcpp.self}").arg("stage").arg("--verify").arg("content")
              .arg("--output").arg(output.c_str()).arg(source.c_str())
              .input(target_file.c_str()).input(source.c_str())
              .output(output.c_str()).submit();
    }

    void copy_file(const path& source, const path& destination) {
        copy(source.lexically_normal().string(), destination);
    }

    void stage_pdb() {
        const auto source = "${mcpp.bin_dir}/" + target + ".pdb";
        const auto output = (release / ".pdb" / (target + ".pdb")).lexically_normal().string();
        const auto id = "stage-pdb-" + std::to_string(next_action++);
        mcpp::action action;
        action.id = id.c_str();
        action.role = "artifact";
        action.arg("${mcpp.self}").arg("stage").arg("--verify").arg("content")
              .arg("--output").arg(output.c_str()).arg(source.c_str())
              .input(target_file.c_str()).output(output.c_str()).submit();
    }

    bool stage_runtime_files(std::string_view member, const path& destination,
                             std::string_view destination_name) {
        const char* tool = mcpp::dep_bin("gpp.runtime-stage", "runtime_stage");
        if (!tool || !*tool) {
            std::println(stderr, "runtime-stage host tool is unavailable");
            return false;
        }
        const auto manifest = release / ".mcpp-runtime" /
            (".mcpp-runtime-" + std::string(member) + "-" +
             std::string(destination_name) + ".txt");
        const auto exe = target_file;
        const auto output = manifest.lexically_normal().string();
        const auto dest = destination.lexically_normal().string();
        const auto id = "runtime-stage-" + std::to_string(next_action++);
        mcpp::action action;
        action.id = id.c_str();
        action.role = "artifact";
        action.arg(tool).arg("--exe").arg(exe.c_str())
              .arg("--manifest").arg(output.c_str())
              .arg("--dest").arg(dest.c_str())
              .input(exe.c_str()).input(tool).output(output.c_str());
        // The vcpkg prefix and the Qt SDK are installed by the build itself
        // (deps-vcpkg, rules-qt-xim), and 7z.dll ships in the xim:7zip payload.
        std::vector<path> search_dirs{
            vcpkg / "bin",
            qt.empty() ? path() : qt / "bin",
            workspace / "3rdParty" / "pybind11" / "bin",
            path(mcpp::xpkg_dir("xim", "7zip")),
        };
        search_dirs.insert(search_dirs.end(), extra_runtime_dirs.begin(), extra_runtime_dirs.end());
        // Every directory is passed, present or not: on a first build the vcpkg
        // prefix is installed by an action after this program has run, and
        // runtime-stage skips a directory that does not exist.
        for (const auto& dir : search_dirs) {
            if (dir.empty()) continue;
            const auto dir_arg = dir.lexically_normal().string();
            action.arg("--search").arg(dir_arg.c_str());
            if (!std::filesystem::is_directory(dir)) continue;
            for (const auto& entry : std::filesystem::directory_iterator(dir)) {
                if (!entry.is_regular_file() || entry.path().extension() != ".dll") continue;
                const auto file = entry.path().lexically_normal().string();
                action.input(file.c_str());
            }
        }
        action.arg("--seed").arg("7z.dll");
        if (member != "Updater") {
            action.arg("--seed").arg("python3.dll");
            action.arg("--seed").arg("python312.dll");
        }
        action.submit();
        return true;
    }

    // The Qt plugins Qt loads by path, which no import table names; the part of
    // windeployqt's work runtime-stage cannot see. The release plugin only: the
    // SDK ships `qwindowsd.dll` beside `qwindows.dll`.
    void copy_qt_plugins(const path& destination) {
        if (qt.empty()) return;   // planned before the SDK is present: nothing to copy yet
        for (const char* dir : {"platforms", "styles", "imageformats"}) {
            std::error_code error;
            for (const auto& entry : std::filesystem::directory_iterator(qt / "plugins" / dir, error)) {
                const auto file = entry.path();
                if (!entry.is_regular_file() || file.extension() != ".dll") continue;
                const auto stem = file.stem().string();
                if (stem.ends_with("d") &&
                    std::filesystem::exists(file.parent_path() / (stem.substr(0, stem.size() - 1) + ".dll")))
                    continue;
                copy_file(file, destination / dir / file.filename());
            }
        }
    }

    void copy_translation_files(std::string_view member, const path& qm,
                                const path& destination) {
        const auto translations = destination / "translations";
        copy_file(qm, translations / qm.filename());
        if (member != "Updater") {
            const auto core_qm = workspace / "GalTranslPP" / "qt_gpp_en.qm";
            copy_file(core_qm, translations / core_qm.filename());
        }
    }

    bool publish_release(std::string_view member, const path& own_qm) {
        if (own_qm.empty()) return false;
        const auto profile = std::string_view(mcpp::profile());
        if (profile != "release" && profile != "fast-release") return true;
        const bool cli = member == "GPPCLI";
        const bool gui = member == "GPPGUI";
        const auto base = release / (cli ? "GPPCLI" : "GPPGUI");
        const auto mirror = private_release_dir(member);
        const bool private_exists = !mirror.empty();
        std::vector<std::pair<path, std::string_view>> destinations{
            {base, cli ? "GPPCLI" : "GPPGUI"}};
        if (gui) destinations.emplace_back(release / "GUICORE", "GUICORE");
        if ((cli || gui) && private_exists) {
            destinations.emplace_back(mirror, cli ? "GPPCLI_PRIVATE" : "GPPGUI_PRIVATE");
        }

        copy(target_file, base / (target + ".exe"));
        if (cli || gui) stage_pdb();
        if (gui) copy(target_file, release / "GUICORE" / (target + ".exe"));
        if (member == "Updater") {
            copy(target_file, release / "GUICORE" / "Updater_new.exe");
            if (private_exists) copy(target_file, mirror / "Updater_new.exe");
        } else if (private_exists) {
            copy(target_file, mirror / (target + ".exe"));
        }

        for (const auto& [dir, name] : destinations)
            if (!stage_runtime_files(member, dir, name)) return false;

        for (const auto& destination : destinations)
            copy_translation_files(member, own_qm, destination.first);
        if (member != "GPPCLI")
            for (const auto& destination : destinations) copy_qt_plugins(destination.first);
        return true;
    }
};
