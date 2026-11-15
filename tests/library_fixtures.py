"""Deterministic, entirely fictional portable-library documents."""
import json


GIT_SYNC_LITERAL = {"format": "Git-Sync data", "version": 1, "exported": "fictional", "filters": ["Empty / Collection"],
    "repos": [{"name": "Fictional / Tool", "site": "github.com", "api": "GitHub", "repository": "Fictional/Tool.git",
        "folder": "inert", "filter": "Utilities / Tools", "keepPreviousVersions": False}]}


def git_sync_document(n=500, *, invalid=False):
    paths = ["Emulators / Nintendo / Switch", "Recomps/Nintendo/N64", "Utilities/Windows", None, ""]
    repositories = []
    for i in range(n):
        key = i - 1 if i and i % 19 == 0 else i
        path = f"Fictional/Repo{key}"
        if i % 4 == 0: path += ".git"
        elif i % 4 == 1: path = path.upper()
        elif i % 4 == 2: path += "/releases"
        entry = {"repository": path, "site": "github.com", "api": "GitHub",
            "name": [None, "", f"Project {i} \u2605 \U0001f680"][i % 3], "filter": paths[i % 5],
            "folder": [None, "", f"inert-folder-{i}"][i % 3], "keepPreviousVersions": [False, True, "false"][i % 3],
            "token": "SECRET_TOKEN", "credentials": {"password": "SECRET_PASSWORD"}, "history": ["SECRET_HISTORY"]}
        if i % 41 == 0: entry.update(api="gitlab", site="gitlab.com", repository=f"Fictional/Subgroup/Repo{i}")
        elif i % 43 == 0: entry.update(api="gitea", site="http://Forge.Example:3000", repository=f"Fictional/Repo{i}")
        elif i % 47 == 0: entry.update(api="forgejo", site="forge.example", repository=f"Fictional/Repo{i}")
        elif i % 53 == 0: entry.update(site="enterprise.example", repository=f"Fictional/Repo{i}")
        repositories.append(entry)
    if invalid: repositories[-1]["repository"] = "https://SECRET_USER:pw@github.com/o/r"
    return {"format": "Git-Sync data", "version": 1, "exported": "fictional", "filters": ["Empty / Collection"],
        "repos": repositories, "token": "SECRET_TOP_LEVEL"}


def native_document(n=500):
    return {"format": "covearchive-repository-list", "version": 1,
        "collections": ["Games/Empty", "Utilities"], "repositories": [
            {"provider": "github", "url": f"https://github.com/Fictional/Repo{i}",
             "name": None, "collection": "Games", "folder": None} for i in range(n)]}


def encoded(document):
    return json.dumps(document, ensure_ascii=True).encode("utf-8")
