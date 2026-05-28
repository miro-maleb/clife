#!/usr/bin/env python3
"""Scan ~/kb/projects and produce a JSON summary of all projects."""
import json
import os
import re
import sys
import yaml

KB_ROOT = os.path.expanduser("~/kb")
PROJECTS_DIR = os.path.join(KB_ROOT, "projects")


def parse_file(filepath):
    with open(filepath) as f:
        text = f.read()
    m = re.match(r"^---\n(.+?)\n---\s*", text, re.DOTALL)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text
    body = text[m.end():]
    return fm, body


def get_title(body):
    for line in body.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return None


def extract_tasks(body):
    tasks = []
    for line in body.split("\n"):
        m = re.match(r"^\s*-\s*\[([ xX])\]\s+(.*)", line)
        if m:
            tasks.append({"text": m.group(2).strip(), "done": m.group(1) != " "})
    return tasks


def scan_subprojects(project_dir):
    subs = []
    for entry in sorted(os.listdir(project_dir)):
        sub_path = os.path.join(project_dir, entry, "sub-project.md")
        if not os.path.isfile(sub_path):
            continue
        fm, body = parse_file(sub_path)
        if not fm:
            continue
        name = get_title(body) or entry.replace("-", " ").title()
        subs.append({
            "slug": entry,
            "name": name,
            "status": str(fm.get("status", "unknown")),
        })
    return subs


def scan_projects():
    projects = []
    for root, dirs, files in os.walk(PROJECTS_DIR):
        if "project.md" not in files:
            continue
        filepath = os.path.join(root, "project.md")
        fm, body = parse_file(filepath)
        if not fm:
            continue

        rel = os.path.relpath(root, PROJECTS_DIR)
        slug = rel.replace(os.sep, "/")
        name = get_title(body) or slug.split("/")[-1].replace("-", " ").title()
        tasks = extract_tasks(body)
        subprojects = scan_subprojects(root)

        projects.append({
            "slug": slug,
            "name": name,
            "path": rel,
            "status": str(fm.get("status", "unknown")),
            "area": str(fm.get("area", slug.split("/")[0])),
            "created": str(fm.get("created", "")),
            "deadline": str(fm.get("deadline", "") or ""),
            "completed": str(fm.get("completed", "") or ""),
            "last_reviewed": str(fm.get("last_reviewed", "") or ""),
            "tags": [str(t) for t in (fm.get("tags") or [])],
            "goals": [str(g) for g in (fm.get("goals") or [])],
            "orientations": [str(o) for o in (fm.get("orientations") or [])],
            "tasks": tasks,
            "subprojects": subprojects,
        })

    return projects


def main():
    projects = scan_projects()
    areas = {}
    for p in projects:
        area = p["area"]
        areas.setdefault(area, []).append(p)

    for area in areas:
        areas[area].sort(key=lambda p: (
            {"active": 0, "on-hold": 1, "sleeping": 2, "complete": 3, "abandoned": 4, "superseded": 5}.get(p["status"], 6),
            p["name"].lower(),
        ))

    data = {"areas": areas, "total": len(projects)}
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            json.dump(data, f, indent=2, default=str)
    else:
        print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
