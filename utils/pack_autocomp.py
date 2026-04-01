import requests

_PYPI_PROJECT_CACHE: list[str] | None = None

def find_pypi_packages(prefix: str) -> list[str]:
    global _PYPI_PROJECT_CACHE

    if not prefix:
        raise ValueError("prefix must be a non-empty string")

    if _PYPI_PROJECT_CACHE is None:
        response = requests.get(
            "https://pypi.org/simple/",
            headers={"Accept": "application/vnd.pypi.simple.v1+json"},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        _PYPI_PROJECT_CACHE = [project["name"] for project in data.get("projects", [])]

    prefix_lower = prefix.lower()
    return sorted(
        [name for name in _PYPI_PROJECT_CACHE if name.lower().startswith(prefix_lower)],
        key=str.lower,
    )

def find_npm_packages(prefix: str, *, size: int = 100) -> list[str]:
    if not prefix:
        raise ValueError("prefix must be a non-empty string")
    if size <= 0:
        raise ValueError("size must be positive")

    response = requests.get(
        "https://registry.npmjs.org/-/v1/search",
        params={"text": prefix, "size": size},
        timeout=5,
    )
    response.raise_for_status()

    data = response.json()
    objects = data.get("objects", [])

    prefix_lower = prefix.lower()
    matches = []

    for obj in objects:
        package = obj.get("package", {})
        name = package.get("name", "")
        if name.lower().startswith(prefix_lower):
            matches.append(name)

    return sorted(set(matches))