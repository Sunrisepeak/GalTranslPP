// The build logic the GalTranslPP members share: the parameters of the mcpp
// plugins they use (the vcpkg triplet, the translation settings), the data the
// programs read at run time from `BaseConfig/` beside themselves, and the
// release layout of the three executable members, Release/GPPCLI,
// Release/GPPGUI, Release/GUICORE and the optional private mirrors.
//
// A module rather than an included header: each build program imports it
// compiled once, it cannot depend on what the program included before it, and
// mcpp rebuilds the programs that import it when it changes.
export module gpp.build;

import std;
import mcpp;
import mcpp.deps;
import mcpp.deps.vcpkg;
import mcpp.deps.archive;
import mcpp.rules.qt;

export namespace gpp {

constexpr const char* triplet = "gpp-x64-windows-release";

// Maps the workspace's vcpkg manifest into this member: the include directory
// always, the listed libraries when the member links them itself, and the
// files of the prefix `deploy` names beside the program.
mcpp::deps::vcpkg::prefix use_vcpkg(std::initializer_list<const char*> libraries = {},
                                    std::vector<mcpp::deps::deploy_entry> deploy = {}) {
    mcpp::deps::vcpkg::options options;
    options.triplet = triplet;
    for (const char* library : libraries) options.libraries.emplace_back(library);
    options.deploy = std::move(deploy);
    return mcpp::deps::vcpkg::use(options);
}

// ─── BaseConfig, beside every program that links the core ─────────────────
//
// The programs open `BaseConfig/...` relative to their own directory. What was
// once two manual steps and Release.py is declared here: the repository's
// Example/BaseConfig, the embedded Python environment from the project's own
// archive (extracted by an action, `mcpp.deps.archive`), and OpenCC's
// dictionaries from the vcpkg prefix. Each file is deployed, so `mcpp run`
// finds it beside the program and `mcpp pack` carries it, and each is copied
// into the release layout.

constexpr const char* python_archive = "Python-3.12.10-embed-amd64.zip";

// The OpenCC files the programs read: `t2s.json` and the two dictionaries it
// names.
std::vector<mcpp::deps::deploy_entry> opencc_files() {
    return {{"share/opencc/t2s.json", "BaseConfig/opencc"},
            {"share/opencc/TSPhrases.ocd2", "BaseConfig/opencc"},
            {"share/opencc/TSCharacters.ocd2", "BaseConfig/opencc"}};
}

// Deploys BaseConfig and returns each file with its path beside the program.
// `opencc` is what `use_vcpkg(..., opencc_files())` deployed.
std::vector<mcpp::deps::deployed_file> base_config(const std::vector<mcpp::deps::deployed_file>& opencc) {
    namespace fs = std::filesystem;
    const fs::path dir = (fs::path(mcpp::manifest_dir()).parent_path() / "Example" / "BaseConfig").lexically_normal();
    std::vector<mcpp::deps::deployed_file> out;

    // The repository's files. The archive is extracted below; the two
    // directories the manual steps used to create are left out if present.
    mcpp::rerun_if_changed_glob("../Example/BaseConfig/**");
    std::vector<fs::path> files;
    std::error_code ec;
    for (auto it = fs::recursive_directory_iterator(dir, ec); it != fs::recursive_directory_iterator(); it.increment(ec)) {
        if (ec) break;
        const auto rel = it->path().lexically_relative(dir);
        const auto top = rel.begin()->string();
        if (it->is_directory(ec)) {
            if (top == "opencc" || top == "Python-3.12.10-embed-amd64") it.disable_recursion_pending();
            continue;
        }
        if (rel == python_archive || !it->is_regular_file(ec)) continue;
        files.push_back(it->path());
    }
    std::ranges::sort(files);
    for (auto const& f : files) {
        const auto rel = f.lexically_relative(dir);
        const std::string to = (fs::path("BaseConfig") / rel.parent_path()).generic_string();
        const std::string from = f.generic_string();
        mcpp::deploy(from.c_str(), to.c_str());
        out.push_back({from, (fs::path("BaseConfig") / rel).generic_string()});
    }

    // The embedded Python environment, from the project's archive.
    mcpp::deps::archive::options python;
    python.archive = (dir / python_archive).generic_string();
    python.to      = "BaseConfig";
    const auto extracted = mcpp::deps::archive::unpack(python);
    out.insert(out.end(), extracted.files.begin(), extracted.files.end());

    out.insert(out.end(), opencc.begin(), opencc.end());
    return out;
}

// Copies BaseConfig into the release layout, as Release.py did: every file into
// Release/GPPCLI and Release/GPPGUI, and into Release/GUICORE all but the
// global configuration, MeCab and the Python environment, which GUICORE takes
// from the GUI it updates.
void publish_base_config(const std::vector<mcpp::deps::deployed_file>& files) {
    namespace fs = std::filesystem;
    const auto profile = std::string_view(mcpp::profile());
    if (profile != "release" && profile != "fast-release") return;
    const fs::path release = fs::path(mcpp::manifest_dir()).parent_path() / "Release";
    auto guicore = [](const std::string& to) {
        return !(to == "BaseConfig/GlobalConfig.toml" || to.starts_with("BaseConfig/mecab/") ||
                 to.starts_with("BaseConfig/Python-3.12.10-embed-amd64/"));
    };
    unsigned n = 0;
    for (const char* member : {"GPPCLI", "GPPGUI", "GUICORE"}) {
        for (auto const& f : files) {
            if (std::string_view(member) == "GUICORE" && !guicore(f.to)) continue;
            const std::string dst = (release / member / f.to).lexically_normal().generic_string();
            const std::string id  = "base-config-" + std::to_string(n++);
            mcpp::action a;
            a.id   = id.c_str();
            a.role = mcpp::roles::artifact;
            a.arg("${mcpp.self}").arg("stage").arg("--verify").arg("content")
             .arg("--output").arg(dst.c_str()).arg(f.path.c_str())
             .input(f.path.c_str()).output(dst.c_str()).submit();
        }
    }
}

// Qt's Visual Studio integration updates the TS file and releases the QM file
// before compiling each project; `update_sources` keeps that order.
mcpp::rules::qt::translations i18n(const char* ts, const char* qm_dir = "") {
    mcpp::rules::qt::translations t;
    t.ts = {ts};
    t.update_sources = true;
    t.tr_function_alias = {"translate+=gppTr"};
    t.out_dir = qm_dir;
    return t;
}

// Where rules-qt writes a member's QM file when `qm_dir` is left empty.
std::filesystem::path qm_path(const char* stem) {
    return std::filesystem::path(mcpp::out_dir()) / "qt" / "translations" / (std::string(stem) + ".qm");
}

// The CLI's sample project, beside the program and in its release directory,
// as Release.py placed it.
void sample_project() {
    namespace fs = std::filesystem;
    const fs::path dir = (fs::path(mcpp::manifest_dir()).parent_path() / "Example" / "SampleProject").lexically_normal();
    mcpp::rerun_if_changed_glob("../Example/SampleProject/**");
    const auto profile = std::string_view(mcpp::profile());
    const bool release = profile == "release" || profile == "fast-release";
    const fs::path out = fs::path(mcpp::manifest_dir()).parent_path() / "Release" / "GPPCLI";
    std::error_code ec;
    unsigned n = 0;
    for (auto it = fs::recursive_directory_iterator(dir, ec); it != fs::recursive_directory_iterator(); it.increment(ec)) {
        if (ec) break;
        if (!it->is_regular_file(ec)) continue;
        const auto rel = it->path().lexically_relative(dir);
        const std::string from = it->path().generic_string();
        const std::string to = (fs::path("SampleProject") / rel.parent_path()).generic_string();
        mcpp::deploy(from.c_str(), to.c_str());
        if (!release) continue;
        const std::string dst = (out / "SampleProject" / rel).lexically_normal().generic_string();
        const std::string id  = "sample-project-" + std::to_string(n++);
        mcpp::action a;
        a.id   = id.c_str();
        a.role = mcpp::roles::artifact;
        a.arg("${mcpp.self}").arg("stage").arg("--verify").arg("content")
         .arg("--output").arg(dst.c_str()).arg(from.c_str())
         .input(from.c_str()).output(dst.c_str()).submit();
    }
}

struct executable_actions {
    using path = std::filesystem::path;

    path project = path(mcpp::manifest_dir());
    path workspace = project.parent_path();
    path release = workspace / "Release";
    path qt = mcpp::rules::qt::root();
    path vcpkg = workspace / "vcpkg_installed" / triplet;
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
            const auto message = "private release directory is unavailable: " + destination.string();
            mcpp::warning(message.c_str());
            return {};
        }
        return destination;
    }

    void copy(std::string source, const path& destination) {
        const auto output = destination.lexically_normal().string();
        const auto id = "stage-" + std::to_string(next_action++);
        mcpp::action action;
        action.id = id.c_str();
        action.role = mcpp::roles::artifact;
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
        action.role = mcpp::roles::artifact;
        action.arg("${mcpp.self}").arg("stage").arg("--verify").arg("content")
              .arg("--output").arg(output.c_str()).arg(source.c_str())
              .input(target_file.c_str()).output(output.c_str()).submit();
    }

    bool stage_runtime_files(std::string_view member, const path& destination,
                             std::string_view destination_name) {
        const char* tool = mcpp::dep_bin("gpp.runtime-stage", "runtime_stage");
        if (!tool || !*tool) {
            mcpp::warning("runtime-stage host tool is unavailable");
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
        action.role = mcpp::roles::artifact;
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
        if (search_dirs[3].empty())
            mcpp::warning("xim:7zip is not declared by this member; the runtime stage cannot find 7z.dll");
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
        // Qt's own strings, which rules-qt combined beside the member's own
        // (`translations::qt_languages`).
        const auto qt_qm = qm.parent_path() / "qt_zh_CN.qm";
        copy_file(qt_qm, translations / qt_qm.filename());
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

} // namespace gpp
