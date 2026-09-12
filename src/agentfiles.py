import difflib
import json
from pathlib import Path


def read_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, None, None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            fields = {}
            for line in lines[1:index]:
                if ":" in line and not line.startswith((" ", "\t", "#")):
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip().strip('"').strip("'")
            return fields, 1, index
    return {}, None, None


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _declared_name(path):
    return read_frontmatter(_read(path))[0].get("name") or path.stem


def installed_plugin_roots(plugins_home):
    manifest = plugins_home / "installed_plugins.json"
    if not manifest.is_file():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    roots = []
    for identifier, installs in sorted((payload.get("plugins") or {}).items()):
        plugin_name = identifier.split("@", 1)[0]
        for install in installs or []:
            install_path = install.get("installPath")
            if not install_path:
                continue
            roots.append({"scope": "plugin", "plugin": plugin_name, "path": Path(install_path), "fallback": False})
    return roots


def marketplace_roots(plugins_home):
    known = plugins_home / "known_marketplaces.json"
    if not known.is_file():
        return []
    try:
        names = sorted(json.loads(known.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    roots = []
    for name in names:
        market = plugins_home / "marketplaces" / name
        if not market.is_dir():
            continue
        for tree in sorted(market.glob("plugins*")):
            if not tree.is_dir():
                continue
            for plugin_dir in sorted(p for p in tree.iterdir() if p.is_dir()):
                roots.append(
                    {"scope": "marketplace", "plugin": plugin_dir.name, "path": plugin_dir, "fallback": True}
                )
    return roots


def default_roots(user_home=None, project_dirs=(), plugins_home=None):
    user_home = Path(user_home) if user_home else Path.home()
    plugins_home = Path(plugins_home) if plugins_home else user_home / ".claude" / "plugins"
    roots = []
    for project in project_dirs:
        if not project:
            continue
        roots.append({"scope": "project", "plugin": None, "path": Path(project) / ".claude", "fallback": False})
    roots.append({"scope": "user", "plugin": None, "path": user_home / ".claude", "fallback": False})
    roots.extend(installed_plugin_roots(plugins_home))
    roots.extend(marketplace_roots(plugins_home))
    return roots


AGENT_DIRS = ("agents",)
SKILL_DIRS = ("skills", "commands")


def _candidate_files(root, kinds):
    found = []
    for kind in kinds:
        directory = root["path"] / kind
        if not directory.is_dir():
            continue
        try:
            found.extend(path for path in directory.rglob("*.md") if path.is_file())
        except OSError:
            continue
    return sorted(found)


def _dedupe(paths):
    seen = {}
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        seen.setdefault(key, path)
    return list(seen.values())


def _split_qualified(name):
    if ":" in name:
        prefix, _, leaf = name.partition(":")
        return prefix, leaf
    return None, name


def _search(roots, kinds, matcher, plugin_prefix):
    primary = []
    fallback = []
    for root in roots:
        if plugin_prefix and root["plugin"] != plugin_prefix:
            continue
        matches = [path for path in _candidate_files(root, kinds) if matcher(path)]
        own = root["fallback"] or (not plugin_prefix and root["plugin"] is not None)
        (fallback if own else primary).extend(matches)
    return _dedupe(primary) or _dedupe(fallback)


def resolve_agent(name, roots):
    prefix, leaf = _split_qualified(name)

    def matcher(path):
        return path.stem == leaf or _declared_name(path) == leaf

    return _search(roots, AGENT_DIRS, matcher, prefix)


def resolve_skill(name, roots):
    prefix, leaf = _split_qualified(name)

    def matcher(path):
        if path.name == "SKILL.md":
            return path.parent.name == leaf or _declared_name(path) == leaf
        return path.stem == leaf and path.parent.name in SKILL_DIRS

    return _search(roots, SKILL_DIRS, matcher, prefix)


def display_path(path, roots):
    for root in roots:
        try:
            return (root["path"].name + "/" + path.relative_to(root["path"]).as_posix()).lstrip("/")
        except ValueError:
            continue
    return path.name


def purpose(text, limit=110):
    cleaned = " ".join((text or "").split())
    for lead in ("Use this agent for ", "Use this agent when ", "Use when ", "CLI tool for ", "CLI for "):
        if cleaned.startswith(lead):
            cleaned = cleaned[len(lead):]
            break
    if not cleaned:
        return ""
    head = cleaned.split(". ")[0].rstrip(".")
    for separator in (": ", " - ", "; "):
        if separator in head:
            head = head.split(separator)[0]
    if len(head) <= limit:
        return head
    return head[:limit].rsplit(" ", 1)[0] + "..."


def propose_model_line(text, target):
    fields, start, end = read_frontmatter(text)
    lines = text.splitlines(keepends=True)
    if start is None:
        return None
    updated = list(lines)
    if "model" in fields:
        for index in range(start, end):
            if lines[index].split(":", 1)[0].strip() == "model":
                updated[index] = "model: %s\n" % target
                break
    else:
        updated.insert(end, "model: %s\n" % target)
    return updated


def unified_patch(path, text, target, label):
    updated = propose_model_line(text, target)
    if updated is None:
        return None
    return "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True), updated, fromfile="a/" + label, tofile="b/" + label, n=1
        )
    )
