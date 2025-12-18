import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown import markdown
from pydantic import BaseModel
from unidecode import unidecode

CONTENT_ROOT = Path("/opt/wiki/content")
INDEX_PATH = Path("/opt/wiki/.index.json")

SERVICE_FILES = {
    "overview.md": "Обзор",
    "passport.md": "Паспорт",
    "architecture.md": "Архитектура",
    "operations.md": "Эксплуатация",
    "incidents.md": "Инциденты",
    "docs.md": "Документация",
    "service-network.md": "Сеть сервиса",
}

ALLOWED_CHILDREN = {
    "root": ["company"],
    "company": ["dc"],
    "dc": ["section", "document", "service", "server", "network"],
    "section": ["section", "document", "service", "server", "network"],
    "document": [],
    "service": [],
    "server": [],
    "network": [],
}

app = FastAPI(title="Internal IT Wiki")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


class CreateRequest(BaseModel):
    parent: Optional[str] = ""
    title: str
    type: str


class SaveRequest(BaseModel):
    path: str
    file: str
    content: str


class ServiceNetworkRequest(BaseModel):
    path: str
    items: List[Dict[str, str]]


def slugify(title: str) -> str:
    base = unidecode(title).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return cleaned or "item"


def ensure_content_root() -> None:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_unique_slug(parent: Path, slug: str) -> str:
    candidate = slug
    counter = 2
    while (parent / candidate).exists():
        candidate = f"{slug}-{counter}"
        counter += 1
    return candidate


def read_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def safe_node_path(path_str: str) -> Path:
    if not path_str:
        return CONTENT_ROOT
    parts = [p for p in path_str.split("/") if p]
    for p in parts:
        if not re.fullmatch(r"[a-z0-9-]+", p):
            raise HTTPException(status_code=400, detail="Некорректный путь")
    target = CONTENT_ROOT
    for p in parts:
        target = target / p
    try:
        resolved = target.resolve(strict=False)
    except Exception:
        raise HTTPException(status_code=400, detail="Путь недоступен")
    if CONTENT_ROOT not in resolved.parents and resolved != CONTENT_ROOT:
        raise HTTPException(status_code=400, detail="Запрещено выходить за пределы хранилища")
    return resolved


def validate_child(parent_type: str, child_type: str) -> None:
    allowed = ALLOWED_CHILDREN.get(parent_type, [])
    if child_type not in allowed:
        if parent_type == "root":
            message = "В корне можно создавать только компании"
        elif parent_type == "company":
            message = "В компании можно создавать только дата-центры"
        elif parent_type in {"dc", "section"}:
            message = "В выбранном узле можно создавать только разделы или объекты"
        else:
            message = "В этом типе узла нельзя создавать потомков"
        raise HTTPException(status_code=400, detail=message)


def load_meta(node_dir: Path, create_default: bool = False) -> Dict:
    meta_path = node_dir / "meta.json"
    if not meta_path.exists() and not create_default:
        raise HTTPException(status_code=404, detail="Узел не найден")
    meta = read_json(meta_path)
    if create_default and not meta:
        now_iso = datetime.utcnow().isoformat()
        meta = {
            "title": node_dir.name,
            "type": "section",
            "slug": node_dir.name,
            "created": now_iso,
            "updated": now_iso,
        }
        write_json(meta_path, meta)
    return meta


def ensure_default_files(node_dir: Path, meta: Dict) -> None:
    index_path = node_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(f"# {meta.get('title', '')}\n", encoding="utf-8")
    if meta.get("type") == "service":
        for file_name in SERVICE_FILES:
            file_path = node_dir / file_name
            if not file_path.exists():
                if file_name == "service-network.md":
                    content = "| Name | IP | Mask | Gateway | DNS |\n| --- | --- | --- | --- | --- |\n"
                else:
                    content = f"# {SERVICE_FILES[file_name]}\n"
                file_path.write_text(content, encoding="utf-8")


def update_meta(node_dir: Path, meta: Dict) -> None:
    meta["updated"] = datetime.utcnow().isoformat()
    write_json(node_dir / "meta.json", meta)


def node_relative_path(node_dir: Path) -> str:
    if node_dir == CONTENT_ROOT:
        return ""
    return str(node_dir.relative_to(CONTENT_ROOT)).replace("\\", "/")


def build_tree(current: Path = CONTENT_ROOT) -> List[Dict]:
    tree = []
    if not current.exists():
        return tree
    folders = [p for p in current.iterdir() if p.is_dir() and (p / "meta.json").exists()]
    folders.sort(key=lambda p: read_json(p / "meta.json").get("title", p.name))
    for child in folders:
        meta = read_json(child / "meta.json")
        node = {
            "title": meta.get("title", child.name),
            "type": meta.get("type", "section"),
            "path": node_relative_path(child),
            "children": build_tree(child)
            if meta.get("type") in {"company", "dc", "section"}
            else [],
        }
        tree.append(node)
    return tree


def render_markdown(content: str) -> str:
    return markdown(
        content,
        extensions=["fenced_code", "tables", "toc", "sane_lists", "codehilite"],
        output_format="html5",
    )


def rebuild_index() -> List[Dict]:
    entries: List[Dict] = []
    if not CONTENT_ROOT.exists():
        return entries
    for meta_path in CONTENT_ROOT.rglob("meta.json"):
        node_dir = meta_path.parent
        meta = read_json(meta_path)
        texts = []
        for md_file in node_dir.glob("*.md"):
            texts.append(md_file.read_text(encoding="utf-8"))
        entries.append(
            {
                "path": node_relative_path(node_dir),
                "title": meta.get("title", ""),
                "type": meta.get("type", ""),
                "content": "\n".join(texts),
                "updated": meta.get("updated"),
            }
        )
    write_json(INDEX_PATH, entries)
    return entries


def load_index() -> List[Dict]:
    if not INDEX_PATH.exists():
        return rebuild_index()
    return read_json(INDEX_PATH)


def auto_parent_path(context_path: Path) -> Path:
    if context_path == CONTENT_ROOT:
        return CONTENT_ROOT
    meta = load_meta(context_path)
    if meta.get("type") in {"document", "service", "server", "network"}:
        return context_path.parent
    return context_path


def create_node(parent_path_str: str, title: str, node_type: str) -> Path:
    parent_dir = safe_node_path(parent_path_str)
    parent_dir = auto_parent_path(parent_dir)
    parent_type = "root" if parent_dir == CONTENT_ROOT else load_meta(parent_dir).get("type", "section")
    validate_child(parent_type, node_type)

    base_slug = slugify(title)
    slug = ensure_unique_slug(parent_dir, base_slug)
    node_dir = parent_dir / slug
    node_dir.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.utcnow().isoformat()
    meta = {
        "title": title,
        "type": node_type,
        "slug": slug,
        "created": now_iso,
        "updated": now_iso,
    }
    if node_type == "service":
        meta["service_network"] = {"items": []}
    write_json(node_dir / "meta.json", meta)
    ensure_default_files(node_dir, meta)
    rebuild_index()
    return node_dir


def ensure_demo_data() -> None:
    if any(CONTENT_ROOT.iterdir()):
        return
    company1 = create_node("", "Первый Дом", "company")
    dc1 = create_node(node_relative_path(company1), "Прохорова", "dc")
    service = create_node(node_relative_path(dc1), "RDS Farm", "service")
    service_dir = service
    service_dir.joinpath("overview.md").write_text("# RDS Farm\nОписание фермы RDS.", encoding="utf-8")
    service_dir.joinpath("passport.md").write_text("## Паспорт сервиса\nОсновные сведения о сервисе.", encoding="utf-8")
    service_dir.joinpath("architecture.md").write_text("## Архитектура\nСхема и зависимостей.", encoding="utf-8")
    service_dir.joinpath("operations.md").write_text("## Эксплуатация\nРегламенты и процессы.", encoding="utf-8")
    service_dir.joinpath("incidents.md").write_text("## Инциденты\nИстория инцидентов и RCA.", encoding="utf-8")
    service_dir.joinpath("docs.md").write_text("## Документация\nСсылки и вложения.", encoding="utf-8")

    company2 = create_node("", "Вторая компания", "company")
    dc2 = create_node(node_relative_path(company2), "Машкова", "dc")
    document = create_node(node_relative_path(dc2), "Первая приемная", "document")
    (CONTENT_ROOT / document / "index.md").write_text("# Первая приемная\nОписание зоны приёма посетителей.", encoding="utf-8")
    rebuild_index()


def list_children(node_dir: Path) -> List[Dict]:
    children = []
    for child in sorted(node_dir.iterdir() if node_dir.exists() else [], key=lambda p: p.name):
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        meta = read_json(meta_path)
        children.append(
            {
                "title": meta.get("title", child.name),
                "type": meta.get("type", "section"),
                "path": node_relative_path(child),
            }
        )
    return children


def breadcrumb(path: Path) -> List[Dict]:
    crumbs = []
    if path == CONTENT_ROOT:
        return crumbs
    current = path
    while current != CONTENT_ROOT:
        meta = load_meta(current)
        crumbs.append({"title": meta.get("title", current.name), "path": node_relative_path(current)})
        current = current.parent
    crumbs.reverse()
    return crumbs


@app.on_event("startup")
async def startup_event() -> None:
    ensure_content_root()
    ensure_demo_data()
    rebuild_index()


@app.get("/api/tree")
async def api_tree() -> Dict:
    return {"tree": build_tree()}


@app.post("/api/create")
async def api_create(payload: CreateRequest):
    node_dir = create_node(payload.parent or "", payload.title, payload.type)
    rel = node_relative_path(node_dir)
    return {"ok": True, "path": rel, "view_url": f"/view/{rel}" if rel else "/"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    tree = build_tree()
    index = load_index()
    companies = [n for n in tree]
    recent = sorted(index, key=lambda x: x.get("updated") or "", reverse=True)[:10]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tree": tree,
            "companies": companies,
            "recent": recent,
            "current_path": "",
        },
    )


@app.get("/view/{path:path}", response_class=HTMLResponse)
async def view_node(path: str, request: Request):
    node_dir = safe_node_path(path)
    if node_dir == CONTENT_ROOT and path:
        raise HTTPException(status_code=404, detail="Узел не найден")
    tree = build_tree()
    if node_dir == CONTENT_ROOT:
        return await dashboard(request)
    meta = load_meta(node_dir)
    ensure_default_files(node_dir, meta)
    children = list_children(node_dir)
    crumb = breadcrumb(node_dir)
    tab = request.query_params.get("tab")
    active_file = "index.md"
    if meta.get("type") == "service":
        if tab and f"{tab}.md" in SERVICE_FILES:
            active_file = f"{tab}.md"
        elif tab == "service-network":
            active_file = "service-network.md"
        else:
            active_file = "overview.md"
    file_path = node_dir / active_file
    content_text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    rendered = render_markdown(content_text)
    auto_overview_needed = active_file == "index.md" and not content_text.strip()
    assets_dir = node_dir / "assets"
    assets = [p.name for p in assets_dir.iterdir()] if assets_dir.exists() else []

    return templates.TemplateResponse(
        "node.html",
        {
            "request": request,
            "tree": tree,
            "meta": meta,
            "path": path,
            "current_path": path,
            "children": children,
            "content": rendered,
            "raw_content": content_text,
            "crumb": crumb,
            "auto_overview": auto_overview_needed,
            "active_file": active_file,
            "service_files": SERVICE_FILES,
            "assets": assets,
        },
    )


@app.get("/edit/{path:path}", response_class=HTMLResponse)
async def edit_node(path: str, request: Request, file: Optional[str] = None):
    node_dir = safe_node_path(path)
    if node_dir == CONTENT_ROOT:
        raise HTTPException(status_code=400, detail="Редактирование корня невозможно")
    meta = load_meta(node_dir)
    ensure_default_files(node_dir, meta)
    filename = file or "index.md"
    if meta.get("type") == "service" and filename not in SERVICE_FILES:
        filename = "overview.md"
    file_path = node_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    content = file_path.read_text(encoding="utf-8")
    tree = build_tree()
    return templates.TemplateResponse(
        "edit.html",
        {
            "request": request,
            "meta": meta,
            "path": path,
            "file": filename,
            "content": content,
            "tree": tree,
            "service_files": SERVICE_FILES,
            "current_path": path,
        },
    )


@app.post("/api/save")
async def api_save(payload: SaveRequest):
    node_dir = safe_node_path(payload.path)
    if node_dir == CONTENT_ROOT:
        raise HTTPException(status_code=400, detail="Нельзя сохранять корень")
    meta = load_meta(node_dir)
    allowed_files = {"index.md"}
    if meta.get("type") == "service":
        allowed_files.update(SERVICE_FILES.keys())
    if payload.file not in allowed_files:
        raise HTTPException(status_code=400, detail="Недопустимое имя файла")
    file_path = node_dir / payload.file
    file_path.write_text(payload.content, encoding="utf-8")
    update_meta(node_dir, meta)
    rebuild_index()
    return {"ok": True}


@app.post("/api/service-network/save")
async def api_service_network(payload: ServiceNetworkRequest):
    node_dir = safe_node_path(payload.path)
    meta = load_meta(node_dir)
    if meta.get("type") != "service":
        raise HTTPException(status_code=400, detail="Форма сети доступна только для сервиса")
    cleaned_items = []
    for item in payload.items:
        cleaned_items.append(
            {
                "name": item.get("name", ""),
                "ip": item.get("ip", ""),
                "mask": item.get("mask", ""),
                "gateway": item.get("gateway", ""),
                "dns": item.get("dns", ""),
            }
        )
    meta.setdefault("service_network", {})["items"] = cleaned_items
    update_meta(node_dir, meta)
    rows = [
        "| Name | IP | Mask | Gateway | DNS |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in cleaned_items:
        rows.append(
            f"| {item['name']} | {item['ip']} | {item['mask']} | {item['gateway']} | {item['dns']} |"
        )
    (node_dir / "service-network.md").write_text("\n".join(rows), encoding="utf-8")
    rebuild_index()
    return {"ok": True}


@app.post("/api/upload")
async def api_upload(path: str = Form(...), file: UploadFile = File(...)):
    node_dir = safe_node_path(path)
    if node_dir == CONTENT_ROOT:
        raise HTTPException(status_code=400, detail="Загрузите файл внутрь узла")
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename)
    assets_dir = node_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    target = assets_dir / filename
    content = await file.read()
    target.write_bytes(content)
    rel_file = str(target.relative_to(CONTENT_ROOT)).replace("\\", "/")
    return {"ok": True, "path": f"/files/{rel_file}"}


@app.get("/files/{path:path}")
async def serve_file(path: str):
    target = CONTENT_ROOT / path
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Файл не найден")
    if CONTENT_ROOT not in resolved.parents:
        raise HTTPException(status_code=400, detail="Запрещенный путь")
    return FileResponse(resolved)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    tree = build_tree()
    results = []
    if q:
        index = load_index()
        ql = q.lower()
        for item in index:
            haystack = f"{item.get('title', '')}\n{item.get('content', '')}".lower()
            if ql in haystack:
                results.append(item)
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "tree": tree, "results": results, "query": q, "current_path": ""},
    )


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": exc.status_code,
            "message": exc.detail,
            "tree": build_tree(),
            "current_path": "",
        },
        status_code=exc.status_code,
    )


@app.get("/health")
async def health():
    return {"ok": True}


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
