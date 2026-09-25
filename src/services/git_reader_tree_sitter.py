"""Tree-sitter based Git repository loading and structural extraction.

Git ingestion flow:
1. Resolve a unique temporary clone path from the repo URL and clone the
   repository shallowly into that working folder. Each ingestion gets its own
   checkout, so concurrent ingestions of the same repo do not collide.
2. Build an authenticated HTTPS clone URL only when a token is supplied. The
   token-bearing URL is used only for the git command and is never logged.
3. Discover supported source/config/docs files while skipping dependency,
   build, cache, and VCS folders such as .git, node_modules, dist, build,
   target, .venv, and __pycache__.
4. Parse supported source files into meaningful units with tree-sitter instead
   of regex:
   - Python: modules, classes, functions, methods, FastAPI/Flask routes.
   - JavaScript/TypeScript/React: classes, functions, arrow functions,
     React components/hooks, interfaces/types, and Express-style routes.
   - Java: classes, interfaces, enums, records, methods, and Spring routes.
5. Parse dependency/runtime/config files that are essential for RAG:
   - requirements.txt and package.json become dependency manifest/package docs.
   - YAML/JSON files become top-level configuration-section documents.
   - Markdown, text, and other special runtime files become fallback documents.
6. Build generated repo intelligence artifacts:
   - repo_knowledge_map.md for human-readable overview and broad retrieval.
   - repo_dependency_graph.json for machine-readable file/symbol/config/route,
     dependency, import, inheritance, and best-effort call relationships.
7. Remove the temporary clone after extraction, even when extraction fails.
8. Return all structured documents to the ingestion pipeline, which chunks
   oversized units, repeats metadata headers on subchunks, embeds the chunks,
   and stores them in Milvus using the bucket's embedding backend.

"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

from src.utils.logger import get_logger

logger = get_logger(__name__)

GIT_CLONE_BASE_PATH = "./data/git_repos"
GIT_INGEST_MAX_FILES = 500
GIT_INGEST_MAX_TOTAL_CHARS = 2_000_000

DEFAULT_EXTENSIONS = [
    ".md",
    ".txt",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".yaml",
    ".yml",
    ".json",
]
SUPPORTED_CODE_EXTENSIONS = [".py", ".js", ".jsx", ".ts", ".tsx", ".java"]
SUPPORTED_EXTENSIONS = DEFAULT_EXTENSIONS
JS_TS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}
SPECIAL_FILENAMES = {
    "dockerfile",
    "makefile",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "pom.xml",
    "build.gradle",
    "compose.yml",
    "docker-compose.yml",
    ".env.example",
}
CODE_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
}
SKIP_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
}


class TreeSitterUnavailable(RuntimeError):
    """Raised when tree-sitter or a required language grammar is unavailable."""


def _repo_name(repo_url: str) -> str:
    """Return a filesystem-friendly repository folder name from a Git URL."""
    return repo_url.rstrip("/").split("/")[-1].replace(".git", "") or "repository"


def _normalise_extensions(file_extensions: Optional[List[str]]) -> List[str]:
    """Normalize optional extension filters to lowercase dot-prefixed values."""
    extensions = file_extensions or DEFAULT_EXTENSIONS
    return [
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in extensions
        if ext
    ]


def _build_authenticated_url(
    repo_url: str,
    username: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    """Inject username/token credentials into HTTPS clone URLs when needed."""
    if not token:
        return repo_url

    parsed = urlparse(repo_url)
    if parsed.scheme not in ("http", "https"):
        logger.warning("Cannot add credentials to non-HTTP Git URL: %s", repo_url)
        return repo_url

    auth_username = username or "oauth2"
    netloc = f"{auth_username}:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    return urlunparse((
        parsed.scheme,
        netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def _safe_error(message: str, token: Optional[str]) -> str:
    """Mask a token from an error message before logging or persisting it."""
    if token:
        message = message.replace(token, "***")
    return message


def _remove_readonly(func, path, _exc_info) -> None:
    """Clear read-only flags and retry rmtree operations on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path: Path, base_path: Path) -> None:
    """Remove a path safely after verifying it lives under the clone base."""
    resolved_path = path.resolve()
    resolved_base = base_path.resolve()
    try:
        resolved_path.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError("Refusing to remove path outside Git clone base") from exc

    try:
        shutil.rmtree(resolved_path, onerror=_remove_readonly)
    except PermissionError:
        import time

        time.sleep(0.5)
        shutil.rmtree(resolved_path, onerror=_remove_readonly)


def _clone_repository(
    repo_url: str,
    branch: Optional[str],
    username: Optional[str],
    token: Optional[str],
) -> Path:
    """Clone a repository into a temporary clone directory and return the path."""
    base_path = Path(GIT_CLONE_BASE_PATH).resolve()
    base_path.mkdir(parents=True, exist_ok=True)
    clone_path = (base_path / f"{_repo_name(repo_url)}-{uuid.uuid4().hex[:8]}").resolve()

    try:
        clone_path.relative_to(base_path)
    except ValueError as exc:
        raise ValueError("Invalid repository clone path") from exc

    if clone_path.exists():
        logger.info("[git-reader-tree-sitter] removing existing clone path=%s", clone_path)
        _remove_tree(clone_path, base_path)

    clone_url = _build_authenticated_url(repo_url, username, token)
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([clone_url, str(clone_path)])

    logger.info(
        "[git-reader-tree-sitter] cloning repository repo=%s branch=%s target=%s auth=%s",
        repo_url,
        branch or "default",
        clone_path,
        "token" if token else "none",
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git CLI is not installed or not available in PATH") from exc

    if result.returncode != 0:
        err = _safe_error(result.stderr or result.stdout or "git clone failed", token)
        raise RuntimeError(err.strip())

    logger.info("[git-reader-tree-sitter] clone complete path=%s", clone_path)
    return clone_path


def _extract_file_paths(
    repo_path: Path,
    extensions: List[str],
    include_special_files: bool = True,
) -> List[Path]:
    """Walk a cloned repo and return files selected for ingestion."""
    file_paths: List[Path] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file_name in files:
            file_path = Path(root) / file_name
            is_special = include_special_files and file_name.lower() in SPECIAL_FILENAMES
            if file_path.suffix.lower() not in extensions and not is_special:
                continue
            file_paths.append(file_path)
            if len(file_paths) >= GIT_INGEST_MAX_FILES:
                logger.info(
                    "[git-reader-tree-sitter] file discovery reached limit files=%d limit=%d",
                    len(file_paths),
                    GIT_INGEST_MAX_FILES,
                )
                return file_paths
    logger.info(
        "[git-reader-tree-sitter] discovered supported files=%d extensions=%s include_special=%s",
        len(file_paths),
        extensions,
        include_special_files,
    )
    return file_paths


def _require_tree_sitter():
    """Import tree-sitter runtime classes or raise an actionable error."""
    try:
        from tree_sitter import Language, Parser
    except Exception as exc:  # noqa: BLE001
        raise TreeSitterUnavailable(
            "tree-sitter is not installed. Install requirements-api.txt first."
        ) from exc
    return Language, Parser


def _language(package_name: str, function_name: str = "language"):
    """Load a tree-sitter language object from an installed grammar package."""
    Language, _ = _require_tree_sitter()
    try:
        module = importlib.import_module(package_name)
        language_fn = getattr(module, function_name)
        return Language(language_fn())
    except Exception as exc:  # noqa: BLE001
        raise TreeSitterUnavailable(
            f"tree-sitter grammar '{package_name}.{function_name}' is unavailable"
        ) from exc


def _parser_for_extension(ext: str):
    """Return a tree-sitter parser configured for a supported code extension."""
    _, Parser = _require_tree_sitter()
    language = {
        ".py": lambda: _language("tree_sitter_python"),
        ".js": lambda: _language("tree_sitter_javascript"),
        ".jsx": lambda: _language("tree_sitter_javascript"),
        ".ts": lambda: _language("tree_sitter_typescript", "language_typescript"),
        ".tsx": lambda: _language("tree_sitter_typescript", "language_tsx"),
        ".java": lambda: _language("tree_sitter_java"),
    }[ext]()

    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        try:
            parser.language = language
        except AttributeError:
            parser.set_language(language)
        return parser


def _node_text(node: Any, source: bytes) -> str:
    """Return the UTF-8 source text covered by a tree-sitter node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")


def _field_text(node: Any, field_name: str, source: bytes) -> Optional[str]:
    """Return text for a named tree-sitter field when it exists."""
    child = node.child_by_field_name(field_name)
    if child is None:
        return None
    return _node_text(child, source).strip()


def _line_span(node: Any) -> tuple[int, int]:
    """Return inclusive 1-based start/end lines for a tree-sitter node."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def _line_segment(lines: List[str], start_line: int, end_line: int) -> str:
    """Return source text for an inclusive 1-based line range."""
    return "\n".join(lines[start_line - 1:end_line])


def _walk(node: Any) -> Iterable[Any]:
    """Yield a tree-sitter node and all descendants depth-first."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _named_children(node: Any) -> Iterable[Any]:
    """Return only named tree-sitter children, skipping punctuation tokens."""
    return (child for child in node.children if child.is_named)


def _first_named_child(node: Any, types: set[str]) -> Optional[Any]:
    """Return the first named child whose node type is in the provided set."""
    for child in _named_children(node):
        if child.type in types:
            return child
    return None


def _base_metadata(
    repo_url: str,
    branch: Optional[str],
    file_path: Path,
    repo_path: Path,
    language: str,
) -> Dict[str, Any]:
    """Build shared metadata for parsed source-code documents."""
    rel_path = file_path.relative_to(repo_path).as_posix()
    metadata = {
        "repo_url": repo_url,
        "branch": branch or "default",
        "source": rel_path,
        "type": "code",
        "file_name": file_path.name,
        "file_path": rel_path,
    }
    metadata["language"] = language
    metadata["parser_backend"] = "tree_sitter"
    return metadata


def _file_metadata(
    repo_url: str,
    branch: Optional[str],
    file_path: Path,
    repo_path: Path,
    doc_type: str = "configuration",
) -> Dict[str, Any]:
    """Build shared metadata for config, text, markdown, and special files."""
    rel_path = file_path.relative_to(repo_path).as_posix()
    metadata = {
        "repo_url": repo_url,
        "branch": branch or "default",
        "source": rel_path,
        "type": doc_type,
        "file_name": file_path.name,
        "file_path": rel_path,
    }
    language = CODE_LANGUAGES.get(file_path.suffix.lower())
    if language:
        metadata["language"] = language
    metadata["parser_backend"] = "tree_sitter"
    return metadata


def _fallback_file_document(content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Create a file-level fallback document when structural parsing is unavailable."""
    return {
        "content": content,
        "metadata": {
            **metadata,
            "unit_type": "file",
            "symbol_name": metadata["file_path"],
            "parser_fallback": True,
            "parser_backend": "tree_sitter",
        },
    }


def _document(
    node: Any,
    source: bytes,
    lines: List[str],
    metadata: Dict[str, Any],
    unit_type: str,
    symbol_name: str,
    parent_symbol: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert a parsed source span into the common document shape."""
    start_line, end_line = _line_span(node)
    doc_metadata = {
        **metadata,
        "unit_type": unit_type,
        "symbol_name": symbol_name,
        "start_line": start_line,
        "end_line": end_line,
    }
    if parent_symbol:
        doc_metadata["parent_symbol"] = parent_symbol
    if extra_metadata:
        doc_metadata.update(extra_metadata)
    return {
        "content": _line_segment(lines, start_line, end_line),
        "metadata": doc_metadata,
    }


def _call_names(root: Any, source: bytes, language: str) -> List[str]:
    """Extract unique function or method call names from a parsed code node."""
    names: List[str] = []
    for node in _walk(root):
        if language == "python" and node.type == "call":
            func = node.child_by_field_name("function")
            name = _call_target_name(func, source)
        elif language in {"javascript", "typescript"} and node.type == "call_expression":
            func = node.child_by_field_name("function")
            name = _call_target_name(func, source)
        elif language == "java" and node.type == "method_invocation":
            name = _field_text(node, "name", source)
        else:
            name = None
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _call_target_name(node: Any, source: bytes) -> Optional[str]:
    """Return the simple name targeted by a tree-sitter call expression node."""
    if node is None:
        return None
    if node.type in {"identifier", "property_identifier", "field_identifier"}:
        return _node_text(node, source).strip()
    name = node.child_by_field_name("name")
    if name is not None:
        return _node_text(name, source).strip()
    property_node = node.child_by_field_name("property")
    if property_node is not None:
        return _node_text(property_node, source).strip()
    return None


def _python_imports(root: Any, source: bytes) -> List[str]:
    """Extract Python import statements from a parsed module tree."""
    imports: List[str] = []
    for node in _walk(root):
        if node.type in {"import_statement", "import_from_statement"}:
            imports.append(_node_text(node, source).strip())
    return imports


def _python_module_summary(
    root: Any,
    source: bytes,
    metadata: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Create a compact Python module document for imports and constants."""
    imports = _python_imports(root, source)
    constants: List[str] = []
    for node in _named_children(root):
        if node.type != "expression_statement":
            continue
        assignment = _first_named_child(node, {"assignment"})
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        if left and left.type == "identifier":
            name = _node_text(left, source).strip()
            if name.isupper():
                constants.append(name)

    if not imports and not constants:
        return None

    summary = [f"Python module: {metadata['file_path']}"]
    if imports:
        summary.extend(["Imports:", *imports[:80]])
    if constants:
        summary.extend(["", "Module constants:", ", ".join(constants[:80])])
    return {
        "content": "\n".join(summary),
        "metadata": {
            **metadata,
            "unit_type": "module",
            "symbol_name": metadata["file_path"],
            "imports": imports,
        },
    }


def _python_decorators(node: Any, source: bytes) -> List[str]:
    """Return decorator text from a decorated Python definition node."""
    if node.type != "decorated_definition":
        return []
    return [
        _node_text(child, source).strip().lstrip("@")
        for child in node.children
        if child.type == "decorator"
    ]


def _route_metadata_from_texts(decorators: List[str]) -> Dict[str, Any]:
    """Detect FastAPI/Flask-style route metadata from decorator strings."""
    for decorator in decorators:
        lowered = decorator.lower()
        for method in ("get", "post", "put", "delete", "patch"):
            if f".{method}" not in lowered:
                continue
            route_path = _first_quoted_string(decorator)
            return {
                "unit_type": "route",
                "http_method": method.upper(),
                "route_path": route_path,
            }
        if ".route" in lowered:
            return {
                "unit_type": "route",
                "http_method": None,
                "route_path": _first_quoted_string(decorator),
            }
    return {}


def _first_quoted_string(text: str) -> Optional[str]:
    """Return the first single- or double-quoted string value in text."""
    for quote in ("'", '"'):
        if quote not in text:
            continue
        try:
            return text.split(quote, 2)[1]
        except IndexError:
            return None
    return None


def _extract_python_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Parse a Python file into module, class, function, method, and route docs."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []

    parser = _parser_for_extension(".py")
    source = content.encode("utf-8")
    tree = parser.parse(source)
    root = tree.root_node
    metadata = _base_metadata(repo_url, branch, file_path, repo_path, "python")
    lines = content.splitlines()
    documents: List[Dict[str, Any]] = []

    module_summary = _python_module_summary(root, source, metadata)
    if module_summary:
        documents.append(module_summary)

    for node in _named_children(root):
        decorators = _python_decorators(node, source)
        declaration = _first_named_child(
            node,
            {"class_definition", "function_definition"},
        ) if node.type == "decorated_definition" else node
        if declaration.type == "class_definition":
            class_name = _field_text(declaration, "name", source)
            if not class_name:
                continue
            documents.append(_document(
                declaration,
                source,
                lines,
                metadata,
                "class",
                class_name,
                extra_metadata={
                    "calls": _call_names(declaration, source, "python"),
                    "inherits": _python_base_classes(declaration, source),
                },
            ))
            body = declaration.child_by_field_name("body")
            if body is None:
                continue
            for item in _named_children(body):
                item_decorators = _python_decorators(item, source)
                method = _first_named_child(item, {"function_definition"}) if item.type == "decorated_definition" else item
                if method.type != "function_definition":
                    continue
                method_name = _field_text(method, "name", source)
                if not method_name:
                    continue
                route_md = _route_metadata_from_texts(item_decorators)
                unit_type = route_md.get("unit_type") or "method"
                documents.append(_document(
                    method,
                    source,
                    lines,
                    metadata,
                    unit_type,
                    f"{class_name}.{method_name}",
                    class_name,
                    {
                        **route_md,
                        "decorators": item_decorators,
                        "calls": _call_names(method, source, "python"),
                    },
                ))
        elif declaration.type == "function_definition":
            function_name = _field_text(declaration, "name", source)
            if not function_name:
                continue
            route_md = _route_metadata_from_texts(decorators)
            unit_type = route_md.get("unit_type") or "function"
            documents.append(_document(
                declaration,
                source,
                lines,
                metadata,
                unit_type,
                function_name,
                extra_metadata={
                    **route_md,
                    "decorators": decorators,
                    "calls": _call_names(declaration, source, "python"),
                },
            ))

    return documents or [_fallback_file_document(content, metadata)]


def _python_base_classes(node: Any, source: bytes) -> List[str]:
    """Extract Python base class expressions from a class definition node."""
    superclasses = node.child_by_field_name("superclasses")
    if superclasses is None:
        return []
    names = []
    for child in _named_children(superclasses):
        text = _node_text(child, source).strip()
        if text and text not in {"(", ")", ","}:
            names.append(text)
    return names


def _js_ts_imports(root: Any, source: bytes) -> List[str]:
    """Extract JavaScript/TypeScript import module specifiers."""
    imports: List[str] = []
    for node in _walk(root):
        if node.type != "import_statement":
            continue
        text = _node_text(node, source).strip()
        source_node = node.child_by_field_name("source")
        imports.append(_node_text(source_node, source).strip("'\"") if source_node else text)
    return list(dict.fromkeys(imports))


def _js_ts_unit_type(name: str, ext: str, fallback: str) -> str:
    """Classify JS/TS symbols as component, hook, function, or type units."""
    if fallback in {"interface", "type"}:
        return fallback
    if ext in {".jsx", ".tsx"} and name[:1].isupper():
        return "react_component"
    if name.startswith("use") and len(name) > 3 and name[3:4].isupper():
        return "react_hook"
    return fallback


def _extract_js_ts_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Extract JS/TS/React classes, functions, types, components, and routes."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []

    ext = file_path.suffix.lower()
    parser = _parser_for_extension(ext)
    source = content.encode("utf-8")
    tree = parser.parse(source)
    root = tree.root_node
    language = "typescript" if ext in {".ts", ".tsx"} else "javascript"
    metadata = _base_metadata(repo_url, branch, file_path, repo_path, language)
    metadata["imports"] = _js_ts_imports(root, source)
    lines = content.splitlines()
    documents: List[Dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()

    for node in _walk(root):
        unit_type: Optional[str] = None
        name: Optional[str] = None
        target_node = node
        extra: Dict[str, Any] = {}

        if node.type == "class_declaration":
            name = _field_text(node, "name", source)
            unit_type = "class"
            extra["inherits"] = _js_ts_extends(node, source)
        elif node.type == "function_declaration":
            name = _field_text(node, "name", source)
            unit_type = _js_ts_unit_type(name or "", ext, "function")
        elif node.type == "interface_declaration":
            name = _field_text(node, "name", source)
            unit_type = "interface"
        elif node.type == "type_alias_declaration":
            name = _field_text(node, "name", source)
            unit_type = "type"
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is None or value.type not in {"arrow_function", "function", "function_expression"}:
                continue
            name = _field_text(node, "name", source)
            unit_type = _js_ts_unit_type(name or "", ext, "function")
        elif node.type == "call_expression":
            route_md = _js_route_metadata(node, source)
            if not route_md:
                continue
            name = f"{route_md.get('http_method', '')} {route_md.get('route_path')}".strip()
            unit_type = "route"
            extra.update(route_md)

        if not name or not unit_type:
            continue
        start_line, end_line = _line_span(target_node)
        key = (start_line, end_line, name)
        if key in seen:
            continue
        seen.add(key)
        extra["calls"] = _call_names(target_node, source, language)
        documents.append(_document(target_node, source, lines, metadata, unit_type, name, extra_metadata=extra))

    return documents or [_fallback_file_document(content, metadata)]


def _js_ts_extends(node: Any, source: bytes) -> List[str]:
    """Extract JavaScript/TypeScript class heritage clauses."""
    names = []
    for child in _named_children(node):
        if child.type in {"class_heritage", "extends_clause"}:
            names.append(_node_text(child, source).replace("extends", "", 1).strip())
    return [name for name in names if name]


def _js_route_metadata(node: Any, source: bytes) -> Dict[str, Any]:
    """Detect Express-style route calls from a call expression node."""
    function_node = node.child_by_field_name("function")
    if function_node is None:
        return {}
    function_text = _node_text(function_node, source).strip()
    lower_text = function_text.lower()
    methods = {"get", "post", "put", "delete", "patch"}
    method = lower_text.rsplit(".", 1)[-1] if "." in lower_text else ""
    owner = lower_text.split(".", 1)[0] if "." in lower_text else ""
    if method not in methods or owner not in {"app", "router"}:
        return {}
    arguments = node.child_by_field_name("arguments")
    route_path = _first_quoted_string(_node_text(arguments, source)) if arguments else None
    return {
        "unit_type": "route",
        "http_method": method.upper(),
        "route_path": route_path,
    }


def _java_imports(root: Any, source: bytes) -> List[str]:
    """Extract Java import declarations from a parsed compilation unit."""
    imports = []
    for node in _walk(root):
        if node.type == "import_declaration":
            text = _node_text(node, source).strip().removeprefix("import").removesuffix(";").strip()
            imports.append(text)
    return list(dict.fromkeys(imports))


def _extract_java_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Extract Java types, methods, constructors, and Spring route handlers."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []

    parser = _parser_for_extension(".java")
    source = content.encode("utf-8")
    tree = parser.parse(source)
    root = tree.root_node
    metadata = _base_metadata(repo_url, branch, file_path, repo_path, "java")
    metadata["imports"] = _java_imports(root, source)
    lines = content.splitlines()
    documents: List[Dict[str, Any]] = []
    type_stack: List[str] = []

    def visit(node: Any) -> None:
        """Visit Java declarations recursively while tracking parent type names."""
        parent_type = type_stack[-1] if type_stack else None
        if node.type in {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}:
            kind = node.type.replace("_declaration", "").replace("class", "class")
            name = _field_text(node, "name", source)
            if name:
                documents.append(_document(
                    node,
                    source,
                    lines,
                    metadata,
                    kind,
                    name,
                    extra_metadata={
                        "calls": _call_names(node, source, "java"),
                        "inherits": _java_inherits(node, source),
                    },
                ))
                type_stack.append(name)
                for child in _named_children(node):
                    visit(child)
                type_stack.pop()
                return

        if node.type in {"method_declaration", "constructor_declaration"}:
            name = _field_text(node, "name", source) or parent_type
            if name:
                annotations = _java_annotations(node, source)
                route_md = _java_route_metadata(annotations)
                unit_type = route_md.get("unit_type") or "method"
                documents.append(_document(
                    node,
                    source,
                    lines,
                    metadata,
                    unit_type,
                    f"{parent_type}.{name}" if parent_type else name,
                    parent_type,
                    {
                        **route_md,
                        "annotations": annotations,
                        "calls": _call_names(node, source, "java"),
                    },
                ))
                return

        for child in _named_children(node):
            visit(child)

    visit(root)
    return documents or [_fallback_file_document(content, metadata)]


def _java_annotations(node: Any, source: bytes) -> List[str]:
    """Collect Java annotations associated with a declaration node."""
    annotations: List[str] = []
    parent = node.parent
    if parent is not None:
        for sibling in parent.children:
            if sibling == node:
                break
            if sibling.type in {"marker_annotation", "annotation"}:
                annotations.append(_node_text(sibling, source).strip())
    for child in node.children:
        if child.type in {"marker_annotation", "annotation"}:
            annotations.append(_node_text(child, source).strip())
    return list(dict.fromkeys(annotations))


def _java_route_metadata(annotations: List[str]) -> Dict[str, Any]:
    """Detect Spring route annotations and return route metadata."""
    method_map = {
        "GetMapping": "GET",
        "PostMapping": "POST",
        "PutMapping": "PUT",
        "DeleteMapping": "DELETE",
        "PatchMapping": "PATCH",
        "RequestMapping": None,
    }
    for annotation in annotations:
        for name, method in method_map.items():
            if name not in annotation:
                continue
            return {
                "unit_type": "route",
                "http_method": method,
                "route_path": _first_quoted_string(annotation),
            }
    return {}


def _java_inherits(node: Any, source: bytes) -> List[str]:
    """Extract Java superclass or interface declarations from a type node."""
    names = []
    for child in _named_children(node):
        if child.type in {"superclass", "super_interfaces"}:
            names.append(_node_text(child, source).strip())
    return names


def _dump_config(data: Any, doc_type: str) -> str:
    """Render config data back to readable JSON/YAML text."""
    if doc_type == "json":
        return json.dumps(data, indent=2)
    try:
        import yaml

        return yaml.dump(data, default_flow_style=False, sort_keys=False)
    except Exception:
        return str(data)


def _extract_config_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Parse JSON/YAML into top-level configuration-section documents."""
    raw = file_path.read_text(encoding="utf-8", errors="ignore")
    if not raw.strip():
        return []

    ext = file_path.suffix.lower()
    doc_type = "json" if ext == ".json" else "yaml"
    metadata = _file_metadata(repo_url, branch, file_path, repo_path, doc_type)
    try:
        if ext == ".json":
            data = json.loads(raw)
        else:
            import yaml

            data = yaml.safe_load(raw)
    except Exception:
        return [_fallback_file_document(raw, metadata)]

    if not isinstance(data, dict):
        return [{
            "content": _dump_config(data, doc_type),
            "metadata": {
                **metadata,
                "unit_type": "configuration",
                "symbol_name": metadata["file_path"],
            },
        }]

    documents = []
    for key, value in data.items():
        documents.append({
            "content": _dump_config({key: value}, doc_type),
            "metadata": {
                **metadata,
                "unit_type": "configuration",
                "symbol_name": str(key),
                "config_key": str(key),
            },
        })
    return documents


def _first_path_segment(path_value: str) -> str:
    """Return the first folder/file segment from a repository-relative path."""
    parts = [part for part in path_value.split("/") if part]
    return parts[0] if parts else "."


def _module_path(path_value: str) -> str:
    """Group a repository-relative path into a compact module path."""
    parts = [part for part in path_value.split("/") if part]
    if len(parts) <= 1:
        return "."
    return "/".join(parts[:2])


def _metadata_line_span(metadata: Dict[str, Any]) -> Optional[str]:
    """Format start/end line metadata as a readable span."""
    if metadata.get("start_line") and metadata.get("end_line"):
        return f"{metadata['start_line']}-{metadata['end_line']}"
    return None


def _table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    """Render rows as a Markdown table."""
    if not rows:
        return []
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value or "") for value in row) + " |")
    return lines


def _trim_text(text: str, max_chars: int = 1200) -> str:
    """Trim long text for README excerpts in the generated knowledge map."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...<truncated>"


def _read_repo_file(repo_path: Path, rel_path: str, max_chars: int = 1200) -> str:
    """Read a repository-relative file excerpt for the knowledge map."""
    try:
        return _trim_text((repo_path / rel_path).read_text(encoding="utf-8", errors="ignore"), max_chars)
    except Exception:
        return ""


def _build_repo_knowledge_map(
    repo_url: str,
    branch: Optional[str],
    repo_path: Path,
    file_paths: List[Path],
    documents: List[Dict[str, Any]],
    extensions: List[str],
) -> Dict[str, Any]:
    """Generate the deterministic Markdown document summarizing repo structure."""
    rel_paths = [path.relative_to(repo_path).as_posix() for path in file_paths]
    ext_counts = Counter(path.suffix.lower() or path.name.lower() for path in file_paths)
    module_counts = Counter(_module_path(path) for path in rel_paths)
    language_counts = Counter(
        doc["metadata"].get("language")
        for doc in documents
        if doc.get("metadata", {}).get("language")
    )
    unit_counts = Counter(
        doc["metadata"].get("unit_type", "unknown")
        for doc in documents
    )
    routes = [
        doc["metadata"] for doc in documents
        if doc.get("metadata", {}).get("unit_type") == "route"
    ]
    symbols = [
        doc["metadata"] for doc in documents
        if doc.get("metadata", {}).get("unit_type") in {
            "class",
            "interface",
            "enum",
            "record",
            "type",
            "function",
            "method",
            "module",
            "react_component",
            "react_hook",
        }
    ]
    configs = [
        doc["metadata"] for doc in documents
        if doc.get("metadata", {}).get("unit_type") == "configuration"
    ]
    dependency_manifests = [
        doc["metadata"] for doc in documents
        if doc.get("metadata", {}).get("unit_type") == "dependency_manifest"
    ]
    fallback_files = sorted({
        doc["metadata"].get("file_path")
        for doc in documents
        if doc.get("metadata", {}).get("parser_fallback")
    })
    dependency_files = sorted(
        path for path in rel_paths
        if Path(path).name.lower() in SPECIAL_FILENAMES
    )
    readme_files = sorted(
        path for path in rel_paths
        if Path(path).name.lower().startswith("readme")
    )

    lines = [
        "# Repository Knowledge Map",
        "",
        "## Repository",
        f"- Repo URL: `{repo_url}`",
        f"- Branch: `{branch or 'default'}`",
        f"- Files scanned: {len(file_paths)}",
        f"- Structured documents generated: {len(documents)}",
        f"- Included extensions: {', '.join(extensions)}",
        "",
        "## Inventory",
        "- File types: " + ", ".join(f"{key}: {value}" for key, value in sorted(ext_counts.items())),
        "- Languages detected: " + (", ".join(f"{key}: {value}" for key, value in sorted(language_counts.items())) or "not detected"),
        "- Unit types: " + ", ".join(f"{key}: {value}" for key, value in sorted(unit_counts.items())),
        "",
        "## Main Modules",
    ]

    for module, count in module_counts.most_common(30):
        top = _first_path_segment(module)
        lines.append(f"- `{module}`: {count} files under `{top}`")

    if routes:
        route_rows = [
            [
                route.get("http_method") or "",
                route.get("route_path") or "",
                route.get("symbol_name") or "",
                route.get("file_path") or "",
                _metadata_line_span(route) or "",
            ]
            for route in routes[:100]
        ]
        lines.extend(["", "## API Routes", *_table(["Method", "Path", "Handler", "File", "Lines"], route_rows)])

    if symbols:
        symbol_rows = [
            [
                symbol.get("unit_type") or "",
                symbol.get("symbol_name") or "",
                symbol.get("parent_symbol") or "",
                symbol.get("file_path") or "",
                _metadata_line_span(symbol) or "",
            ]
            for symbol in symbols[:200]
        ]
        lines.extend(["", "## Important Symbols", *_table(["Type", "Symbol", "Parent", "File", "Lines"], symbol_rows)])

    if configs:
        config_rows = [
            [
                config.get("config_key") or config.get("symbol_name") or "",
                config.get("file_path") or "",
            ]
            for config in configs[:150]
        ]
        lines.extend(["", "## Configuration Sections", *_table(["Key", "File"], config_rows)])

    if dependency_manifests:
        lines.extend(["", "## Dependencies"])
        for manifest in dependency_manifests:
            package_names = manifest.get("package_names") or []
            lines.append(f"### `{manifest.get('file_path')}`")
            for package_name in package_names[:200]:
                lines.append(f"- `{package_name}`")

    if dependency_files:
        lines.extend(["", "## Dependency And Runtime Files"])
        for rel_path in dependency_files[:30]:
            lines.append(f"- `{rel_path}`")

    if readme_files:
        lines.extend(["", "## README Extracts"])
        for rel_path in readme_files[:3]:
            excerpt = _read_repo_file(repo_path, rel_path)
            if excerpt:
                lines.extend([f"### `{rel_path}`", "", excerpt, ""])

    if fallback_files:
        lines.extend(["", "## Parser Fallback Files"])
        for rel_path in fallback_files[:100]:
            lines.append(f"- `{rel_path}`")

    return {
        "content": "\n".join(lines).strip(),
        "metadata": {
            "repo_url": repo_url,
            "branch": branch or "default",
            "source": "repo_knowledge_map.md",
            "type": "markdown",
            "file_name": "repo_knowledge_map.md",
            "file_path": "repo_knowledge_map.md",
            "unit_type": "repo_knowledge_map",
            "symbol_name": "Repository Knowledge Map",
            "generated": True,
            "parser_backend": "tree_sitter",
        },
    }


def _write_knowledge_map(repo_path: Path, knowledge_map: Dict[str, Any]) -> None:
    """Best-effort write of repo_knowledge_map.md into the cloned repo folder."""
    try:
        (repo_path / "repo_knowledge_map.md").write_text(
            knowledge_map["content"],
            encoding="utf-8",
        )
        logger.info("[git-reader-tree-sitter] wrote repo knowledge map path=%s", repo_path / "repo_knowledge_map.md")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[git_reader_tree_sitter] could not write repo knowledge map: %s", exc)


def _node_id(node_type: str, name: str) -> str:
    """Build a stable graph node id."""
    return f"{node_type}:{name}"


def _add_graph_node(nodes: Dict[str, Dict[str, Any]], node: Dict[str, Any]) -> None:
    """Add or merge a graph node by id."""
    node_id = node["id"]
    if node_id in nodes:
        nodes[node_id].update({k: v for k, v in node.items() if v is not None})
        return
    nodes[node_id] = {k: v for k, v in node.items() if v is not None}


def _add_graph_edge(edges: set[tuple[str, str, str]], source: str, target: str, edge_type: str) -> None:
    """Add a deduplicated directed graph edge."""
    if source and target and source != target:
        edges.add((source, target, edge_type))


def _normalize_import_name(import_name: str) -> str:
    """Convert parser-specific import text into a resolvable import name."""
    text = import_name.strip().rstrip(";")
    for prefix in ("import ", "from "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if " import " in text:
        text = text.split(" import ", 1)[0]
    if " from " in text:
        text = text.rsplit(" from ", 1)[-1]
    return text.strip().strip("'\"")


def _imports_by_file(documents: List[Dict[str, Any]]) -> dict[str, List[str]]:
    """Collect import references from document metadata by repository file path."""
    imports: dict[str, List[str]] = {}
    for doc in documents:
        metadata = doc.get("metadata", {})
        file_path = metadata.get("file_path")
        if not file_path:
            continue
        values = metadata.get("imports") or []
        normalized = [_normalize_import_name(str(value)) for value in values if value]
        if normalized:
            imports.setdefault(file_path, []).extend(normalized)
    return {
        file_path: list(dict.fromkeys(values))
        for file_path, values in imports.items()
    }


def _build_file_lookup(file_paths: List[Path], repo_path: Path) -> dict[str, str]:
    """Build lookup keys that help resolve imports to local repository files."""
    lookup: dict[str, str] = {}
    for path in file_paths:
        rel_path = path.relative_to(repo_path).as_posix()
        no_ext = str(Path(rel_path).with_suffix("")).replace("\\", "/")
        dotted = no_ext.replace("/", ".")
        lookup[rel_path] = rel_path
        lookup[no_ext] = rel_path
        lookup[dotted] = rel_path
        lookup[Path(rel_path).stem] = rel_path
        if rel_path.endswith("/__init__.py"):
            package = rel_path[:-len("/__init__.py")]
            lookup[package] = rel_path
            lookup[package.replace("/", ".")] = rel_path
        if Path(rel_path).name in {"index.js", "index.jsx", "index.ts", "index.tsx"}:
            package = str(Path(rel_path).parent).replace("\\", "/")
            lookup[package] = rel_path
    return lookup


def _resolve_import_target(
    import_name: str,
    source_rel_path: str,
    file_lookup: dict[str, str],
) -> Optional[str]:
    """Resolve an import string to a local repository file when possible."""
    if not import_name:
        return None

    normalized = import_name.replace("\\", "/").strip()
    candidates = [normalized, normalized.lstrip(".").replace(".", "/"), normalized.lstrip(".")]

    source_dir = Path(source_rel_path).parent
    if normalized.startswith("."):
        rel_candidate = (source_dir / normalized).as_posix()
        candidates.extend([
            rel_candidate,
            str(Path(rel_candidate).with_suffix("")).replace("\\", "/"),
        ])

    extensions = ["", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", "/index.js", "/index.jsx", "/index.ts", "/index.tsx", "/__init__.py"]
    for candidate in candidates:
        for suffix in extensions:
            key = f"{candidate}{suffix}"
            if key in file_lookup:
                return file_lookup[key]
            dotted = key.replace("/", ".")
            if dotted in file_lookup:
                return file_lookup[dotted]
    return None


def _build_symbol_lookup(documents: List[Dict[str, Any]]) -> dict[str, list[Dict[str, Any]]]:
    """Index symbols by simple and qualified names for call resolution."""
    lookup: dict[str, list[Dict[str, Any]]] = {}
    for doc in documents:
        metadata = doc.get("metadata", {})
        if metadata.get("unit_type") not in {
            "class",
            "interface",
            "enum",
            "record",
            "type",
            "function",
            "method",
            "route",
            "react_component",
            "react_hook",
        }:
            continue
        symbol_name = metadata.get("symbol_name")
        file_path = metadata.get("file_path")
        if not symbol_name or not file_path:
            continue
        simple_name = str(symbol_name).split(".")[-1]
        for key in {symbol_name, simple_name, f"{file_path}:{symbol_name}"}:
            lookup.setdefault(str(key), []).append(metadata)
    return lookup


def _best_symbol_match(
    call_name: str,
    source_file: str,
    symbol_lookup: dict[str, list[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Choose the most likely symbol target for a call name."""
    matches = symbol_lookup.get(call_name) or []
    if not matches:
        return None
    for match in matches:
        if match.get("file_path") == source_file:
            return match
    return matches[0]


def _inheritance_targets(metadata: Dict[str, Any]) -> List[str]:
    """Return declared inheritance targets from structured metadata."""
    inherits = metadata.get("inherits") or []
    if isinstance(inherits, str):
        inherits = [inherits]
    return [item for item in inherits if item]


def _call_targets(metadata: Dict[str, Any]) -> List[str]:
    """Return call targets extracted by tree-sitter into metadata."""
    calls = metadata.get("calls") or []
    return [str(call) for call in calls if call]


def _build_repo_dependency_graph(
    repo_url: str,
    branch: Optional[str],
    repo_path: Path,
    file_paths: List[Path],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Generate a machine-readable repository dependency graph document."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()
    file_lookup = _build_file_lookup(file_paths, repo_path)
    symbol_lookup = _build_symbol_lookup(documents)
    imports_by_file = _imports_by_file(documents)

    repo_id = _node_id("repo", repo_url)
    _add_graph_node(nodes, {
        "id": repo_id,
        "type": "repo",
        "name": repo_url,
        "branch": branch or "default",
    })

    for file_path in file_paths:
        rel_path = file_path.relative_to(repo_path).as_posix()
        file_id = _node_id("file", rel_path)
        _add_graph_node(nodes, {
            "id": file_id,
            "type": "file",
            "name": rel_path,
            "extension": file_path.suffix.lower() or file_path.name.lower(),
            "special_file": file_path.name.lower() in SPECIAL_FILENAMES,
        })
        _add_graph_edge(edges, repo_id, file_id, "contains_file")

        for import_name in imports_by_file.get(rel_path, []):
            resolved_file = _resolve_import_target(import_name, rel_path, file_lookup)
            if resolved_file:
                _add_graph_edge(edges, file_id, _node_id("file", resolved_file), "imports_local")
                continue
            import_id = _node_id("external_or_import", import_name)
            _add_graph_node(nodes, {
                "id": import_id,
                "type": "external_or_import",
                "name": import_name,
            })
            _add_graph_edge(edges, file_id, import_id, "imports_external_or_unresolved")

    for doc in documents:
        metadata = doc.get("metadata", {})
        file_path_value = metadata.get("file_path")
        unit_type = metadata.get("unit_type")
        symbol_name = metadata.get("symbol_name")
        if not file_path_value or not unit_type or unit_type == "repo_knowledge_map":
            continue

        file_id = _node_id("file", file_path_value)
        if unit_type == "configuration":
            config_key = metadata.get("config_key") or symbol_name
            config_id = _node_id("config", f"{file_path_value}:{config_key}")
            _add_graph_node(nodes, {
                "id": config_id,
                "type": "config",
                "name": config_key,
                "file_path": file_path_value,
            })
            _add_graph_edge(edges, file_id, config_id, "defines_config")
            continue

        if unit_type == "dependency":
            package_name = metadata.get("package_name") or symbol_name
            dependency_id = _node_id("dependency", str(package_name))
            _add_graph_node(nodes, {
                "id": dependency_id,
                "type": "dependency",
                "name": package_name,
                "manager": metadata.get("dependency_manager"),
                "specifier": metadata.get("dependency_specifier"),
            })
            _add_graph_edge(edges, file_id, dependency_id, "declares_dependency")
            continue

        if not symbol_name:
            continue
        symbol_id = _node_id("symbol", f"{file_path_value}:{symbol_name}")
        _add_graph_node(nodes, {
            "id": symbol_id,
            "type": "symbol",
            "name": symbol_name,
            "unit_type": unit_type,
            "file_path": file_path_value,
            "language": metadata.get("language"),
            "start_line": metadata.get("start_line"),
            "end_line": metadata.get("end_line"),
            "route_path": metadata.get("route_path"),
            "http_method": metadata.get("http_method"),
        })
        _add_graph_edge(edges, file_id, symbol_id, "defines_symbol")

        parent_symbol = metadata.get("parent_symbol")
        if parent_symbol:
            parent_id = _node_id("symbol", f"{file_path_value}:{parent_symbol}")
            _add_graph_node(nodes, {
                "id": parent_id,
                "type": "symbol",
                "name": parent_symbol,
                "file_path": file_path_value,
            })
            _add_graph_edge(edges, parent_id, symbol_id, "contains_symbol")

        if unit_type == "route":
            route_name = f"{metadata.get('http_method') or ''} {metadata.get('route_path') or symbol_name}".strip()
            route_id = _node_id("route", f"{file_path_value}:{route_name}")
            _add_graph_node(nodes, {
                "id": route_id,
                "type": "route",
                "name": route_name,
                "file_path": file_path_value,
                "http_method": metadata.get("http_method"),
                "route_path": metadata.get("route_path"),
            })
            _add_graph_edge(edges, route_id, symbol_id, "handled_by")

        for base_name in _inheritance_targets(metadata):
            target_metadata = _best_symbol_match(base_name, file_path_value, symbol_lookup)
            if target_metadata:
                target_id = _node_id(
                    "symbol",
                    f"{target_metadata.get('file_path')}:{target_metadata.get('symbol_name')}",
                )
                _add_graph_edge(edges, symbol_id, target_id, "inherits")
            else:
                base_id = _node_id("external_or_symbol", base_name)
                _add_graph_node(nodes, {
                    "id": base_id,
                    "type": "external_or_symbol",
                    "name": base_name,
                })
                _add_graph_edge(edges, symbol_id, base_id, "inherits_external_or_unresolved")

        if unit_type in {"function", "method", "route", "react_component", "react_hook"}:
            for call_name in _call_targets(metadata):
                target_metadata = _best_symbol_match(call_name, file_path_value, symbol_lookup)
                if target_metadata:
                    target_id = _node_id(
                        "symbol",
                        f"{target_metadata.get('file_path')}:{target_metadata.get('symbol_name')}",
                    )
                    _add_graph_edge(edges, symbol_id, target_id, "calls")
                    continue
                call_id = _node_id("external_or_call", call_name)
                _add_graph_node(nodes, {
                    "id": call_id,
                    "type": "external_or_call",
                    "name": call_name,
                })
                _add_graph_edge(edges, symbol_id, call_id, "calls_external_or_unresolved")

    node_type_counts = Counter(node.get("type", "unknown") for node in nodes.values())
    edge_type_counts = Counter(edge_type for _, _, edge_type in edges)
    graph = {
        "repo_url": repo_url,
        "branch": branch or "default",
        "generated": True,
        "schema_version": 2,
        "parser_backend": "tree_sitter",
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "file_count": len(file_paths),
            "structured_document_count": len(documents),
            "node_types": dict(sorted(node_type_counts.items())),
            "edge_types": dict(sorted(edge_type_counts.items())),
        },
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": [
            {"from": source, "to": target, "type": edge_type}
            for source, target, edge_type in sorted(edges)
        ],
    }
    return {
        "content": json.dumps(graph, indent=2, sort_keys=True),
        "metadata": {
            "repo_url": repo_url,
            "branch": branch or "default",
            "source": "repo_dependency_graph.json",
            "type": "json",
            "file_name": "repo_dependency_graph.json",
            "file_path": "repo_dependency_graph.json",
            "unit_type": "repo_dependency_graph",
            "symbol_name": "Repository Dependency Graph",
            "generated": True,
            "parser_backend": "tree_sitter",
        },
    }


def _write_dependency_graph(repo_path: Path, dependency_graph: Dict[str, Any]) -> None:
    """Best-effort write of repo_dependency_graph.json into the cloned repo folder."""
    try:
        (repo_path / "repo_dependency_graph.json").write_text(
            dependency_graph["content"],
            encoding="utf-8",
        )
        logger.info("[git-reader-tree-sitter] wrote repo dependency graph path=%s", repo_path / "repo_dependency_graph.json")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[git_reader_tree_sitter] could not write repo dependency graph: %s", exc)


def _parse_requirement_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one pip requirements.txt line into dependency metadata."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
        return {"name": stripped.split(maxsplit=1)[-1], "specifier": stripped, "kind": "include"}
    if stripped.startswith(("-e ", "--editable ")):
        return {"name": stripped.split(maxsplit=1)[-1], "specifier": stripped, "kind": "editable"}
    if stripped.startswith(("--index-url", "--extra-index-url", "--find-links", "-f ")):
        return None

    requirement = stripped.split("#", 1)[0].strip()
    requirement = requirement.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)\s*(.*)$", requirement)
    if not match:
        return {"name": requirement, "specifier": stripped, "kind": "raw"}
    return {
        "name": match.group(1),
        "specifier": match.group(2).strip(),
        "kind": "python_package",
    }


def _extract_requirements_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Parse requirements.txt into a manifest doc and per-package documents."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []

    metadata = _file_metadata(repo_url, branch, file_path, repo_path, "configuration")
    lines = content.splitlines()
    dependencies = []
    for line_number, line in enumerate(lines, start=1):
        parsed = _parse_requirement_line(line)
        if parsed:
            dependencies.append({"line": line_number, **parsed})

    if not dependencies:
        return [_fallback_file_document(content, metadata)]

    package_names = [dep["name"] for dep in dependencies]
    summary_lines = [
        f"Python dependency manifest: {metadata['file_path']}",
        "Dependency file: requirements.txt",
        "Packages:",
        *[
            f"- {dep['name']}{(' ' + dep['specifier']) if dep.get('specifier') else ''}"
            for dep in dependencies
        ],
    ]
    documents = [{
        "content": "\n".join(summary_lines),
        "metadata": {
            **metadata,
            "unit_type": "dependency_manifest",
            "symbol_name": metadata["file_path"],
            "dependency_manager": "pip",
            "dependency_file": "requirements.txt",
            "package_names": package_names,
        },
    }]

    for dep in dependencies:
        documents.append({
            "content": "\n".join([
                f"Dependency: {dep['name']}",
                f"Dependency file: {metadata['file_path']}",
                f"Specifier: {dep.get('specifier') or 'unversioned'}",
                f"Kind: {dep.get('kind')}",
            ]),
            "metadata": {
                **metadata,
                "unit_type": "dependency",
                "symbol_name": dep["name"],
                "package_name": dep["name"],
                "dependency_specifier": dep.get("specifier"),
                "dependency_manager": "pip",
                "start_line": dep["line"],
                "end_line": dep["line"],
            },
        })

    return documents


def _extract_package_json_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Parse package.json dependency sections into dependency documents."""
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []

    metadata = _file_metadata(repo_url, branch, file_path, repo_path, "configuration")
    try:
        data = json.loads(content)
    except Exception:  # noqa: BLE001
        return [_fallback_file_document(content, metadata)]

    dependencies = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = data.get(section) or {}
        if isinstance(values, dict):
            for name, specifier in values.items():
                dependencies.append({
                    "name": str(name),
                    "specifier": str(specifier),
                    "section": section,
                })

    if not dependencies:
        return [_fallback_file_document(content, metadata)]

    documents = [{
        "content": "\n".join([
            f"JavaScript dependency manifest: {metadata['file_path']}",
            "Dependency file: package.json",
            "Packages:",
            *[f"- {dep['name']} {dep['specifier']} ({dep['section']})" for dep in dependencies],
        ]),
        "metadata": {
            **metadata,
            "unit_type": "dependency_manifest",
            "symbol_name": metadata["file_path"],
            "dependency_manager": "npm",
            "dependency_file": "package.json",
            "package_names": [dep["name"] for dep in dependencies],
        },
    }]
    for dep in dependencies:
        documents.append({
            "content": "\n".join([
                f"Dependency: {dep['name']}",
                f"Dependency file: {metadata['file_path']}",
                f"Specifier: {dep['specifier']}",
                f"Section: {dep['section']}",
            ]),
            "metadata": {
                **metadata,
                "unit_type": "dependency",
                "symbol_name": dep["name"],
                "package_name": dep["name"],
                "dependency_specifier": dep["specifier"],
                "dependency_section": dep["section"],
                "dependency_manager": "npm",
            },
        })
    return documents


def _load_special_file_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Load special runtime/dependency files with dedicated parsers when available."""
    filename = file_path.name.lower()
    if filename == "requirements.txt":
        return _extract_requirements_documents(file_path, repo_path, repo_url, branch)
    if filename == "package.json":
        return _extract_package_json_documents(file_path, repo_path, repo_url, branch)

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []
    metadata = _file_metadata(repo_url, branch, file_path, repo_path, "configuration")
    return [_fallback_file_document(content, metadata)]


def _append_dependency_section(
    knowledge_map: Dict[str, Any],
    documents: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Append dependency package names to a knowledge map if not already present."""
    if "## Dependencies" in knowledge_map.get("content", ""):
        return knowledge_map
    manifests = [
        doc for doc in documents
        if doc.get("metadata", {}).get("unit_type") == "dependency_manifest"
    ]
    if not manifests:
        return knowledge_map

    lines = [knowledge_map.get("content", "").rstrip(), "", "## Dependencies"]
    for doc in manifests:
        metadata = doc.get("metadata", {})
        package_names = metadata.get("package_names") or []
        if not package_names:
            continue
        lines.append(f"### `{metadata.get('file_path')}`")
        for package_name in package_names:
            lines.append(f"- `{package_name}`")
    return {
        **knowledge_map,
        "content": "\n".join(lines).strip(),
    }


def _load_structured_documents(
    file_path: Path,
    repo_path: Path,
    repo_url: str,
    branch: Optional[str],
) -> List[Dict[str, Any]]:
    """Choose the right tree-sitter/config/special-file parser for a file."""
    ext = file_path.suffix.lower()
    if file_path.name.lower() in SPECIAL_FILENAMES:
        return _load_special_file_documents(file_path, repo_path, repo_url, branch)
    if ext == ".py":
        return _extract_python_documents(file_path, repo_path, repo_url, branch)
    if ext in JS_TS_EXTENSIONS:
        return _extract_js_ts_documents(file_path, repo_path, repo_url, branch)
    if ext == ".java":
        return _extract_java_documents(file_path, repo_path, repo_url, branch)
    if ext in {".yaml", ".yml", ".json"}:
        return _extract_config_documents(file_path, repo_path, repo_url, branch)

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    if not content.strip():
        return []
    metadata = _file_metadata(
        repo_url,
        branch,
        file_path,
        repo_path,
        "markdown" if ext == ".md" else "text" if ext == ".txt" else "configuration",
    )
    return [_fallback_file_document(content, metadata)]


def _documents_from_clone(
    repo_url: str,
    clone_path: Path,
    branch: Optional[str],
    file_extensions: Optional[List[str]],
) -> tuple[List[Dict[str, Any]], int]:
    """Extract all documents ready for vector ingestion from a cloned repo."""
    requested_extensions = _normalise_extensions(file_extensions)
    extensions = [ext for ext in requested_extensions if ext in SUPPORTED_EXTENSIONS]
    if not extensions:
        extensions = DEFAULT_EXTENSIONS

    file_paths = _extract_file_paths(
        clone_path,
        extensions,
        include_special_files=True,
    )
    documents: List[Dict[str, Any]] = []
    total_chars = 0
    files_processed = 0

    for file_path in file_paths:
        try:
            file_docs = _load_structured_documents(file_path, clone_path, repo_url, branch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[git_reader_tree_sitter] skipping file %s: %s", file_path, exc)
            continue

        accepted_docs = []
        for doc in file_docs:
            content_len = len(doc.get("content", ""))
            if total_chars + content_len > GIT_INGEST_MAX_TOTAL_CHARS:
                logger.info("[git_reader_tree_sitter] reached repository content limit for %s", repo_url)
                break
            accepted_docs.append(doc)
            total_chars += content_len

        if accepted_docs:
            files_processed += 1
            documents.extend(accepted_docs)

        if total_chars >= GIT_INGEST_MAX_TOTAL_CHARS:
            break

    if not documents:
        raise ValueError("No supported tree-sitter files found in repository")

    knowledge_map = _build_repo_knowledge_map(
        repo_url,
        branch,
        clone_path,
        file_paths,
        documents,
        extensions,
    )
    knowledge_map = _append_dependency_section(knowledge_map, documents)
    knowledge_map["metadata"]["parser_backend"] = "tree_sitter"
    _write_knowledge_map(clone_path, knowledge_map)
    documents.insert(0, knowledge_map)

    dependency_graph = _build_repo_dependency_graph(
        repo_url,
        branch,
        clone_path,
        file_paths,
        documents,
    )
    _write_dependency_graph(clone_path, dependency_graph)
    documents.insert(1, dependency_graph)

    return documents, files_processed


def _cleanup_clone(clone_path: Path) -> None:
    """Best-effort cleanup of the temporary repository clone."""
    try:
        if clone_path.exists():
            logger.info("[git-reader-tree-sitter] cleaning up temporary clone path=%s", clone_path)
            _remove_tree(clone_path, Path(GIT_CLONE_BASE_PATH).resolve())
            logger.info("[git-reader-tree-sitter] temporary clone removed path=%s", clone_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[git_reader_tree_sitter] temporary clone cleanup failed path=%s: %s", clone_path, exc)


def load_repository_documents(
    repo_url: str,
    branch: Optional[str] = None,
    file_extensions: Optional[List[str]] = None,
    username: Optional[str] = None,
    token: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], int]:
    """Clone a repository and return tree-sitter structured documents."""
    logger.info("[git-reader-tree-sitter] repository extraction started repo=%s", repo_url)
    clone_path = _clone_repository(repo_url, branch, username, token)
    try:
        return _documents_from_clone(repo_url, clone_path, branch, file_extensions)
    finally:
        _cleanup_clone(clone_path)
