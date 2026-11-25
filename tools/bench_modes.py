"""Measure the local PDF readers against archived quality conversions."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
from importlib.metadata import version
from io import StringIO
import json
import os
from pathlib import Path
import re
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "ocr_baseline.md"
MODES = {"fast": "Быстрый", "balanced": "Сбалансированный", "quality": "Точный"}
TOKEN = re.compile(r"\w+|[^\w\s]")
PARAGRAPH_TAG = re.compile(r"^\s*<p(?:\s[^>]*)?>", re.IGNORECASE)
MATH_TAG = re.compile(r"<math\b", re.IGNORECASE)


def sha256_file(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def revision():
    head = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    digest = hashlib.sha256()
    for path in sorted([*ROOT.glob("*.py"), *(ROOT / "local_web").glob("*.py")]):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return head, digest.hexdigest()


def html_counts(body):
    images = body.find_all("img")
    labels = [(image.get("alt") or "").strip() for image in images]
    math = body.find_all("math")
    text = body.select(
        "[data-formula]:not(:has(math)), code[data-source-bbox][data-source-page]:not(:has(math))"
    )
    latex = sum(node.has_attr("data-latex") or "math-latex" in node.get("class", []) for node in text)
    return dict(
        tables=len(body.find_all("table")),
        cells=len(body.find_all(["td", "th"])),
        images=len(images),
        mathml=len(math),
        formula_text=len(text) - latex,
        latex=latex,
        formula_png=sum(label == "Формула" or label.startswith("Формула ") for label in labels),
        page_images=labels.count("Исходная страница PDF"),
        audit_images=labels.count("Фрагмент исходной ячейки для сверки"),
        linked_formula_crops=sum(node.has_attr("data-source-formula-crop") for node in math),
    )


def markdown_leaks(value):
    from markdown_it import MarkdownIt

    counts = dict(paragraph=0, math=0, total=0)
    for block in MarkdownIt("commonmark").parse(value):
        for token in block.children or ():
            if token.type == "code_inline":
                paragraph = bool(PARAGRAPH_TAG.search(token.content))
                math = bool(MATH_TAG.search(token.content))
                counts["paragraph"] += paragraph
                counts["math"] += math
                counts["total"] += paragraph or math
    return counts


def token_distance(text, reference):
    return 1.0 - SequenceMatcher(None, TOKEN.findall(reference), TOKEN.findall(text), autojunk=False).ratio()


def document_metrics(canonical, markdown, reference=None):
    from bs4 import BeautifulSoup

    body = BeautifulSoup(canonical.read_bytes(), "html.parser").body
    if body is None:
        raise ValueError(f"В каноническом HTML нет body: {canonical}")
    text = body.get_text(" ", strip=True)
    result = html_counts(body)
    result["tokens"] = len(TOKEN.findall(text))
    result["text_gap"] = token_distance(text, reference) if reference is not None else 0.0
    result["leaks"] = markdown_leaks(markdown.read_text(encoding="utf-8")) if markdown else None
    return result, text


def historical_timing(lines):
    started = last = None
    for line in lines:
        match = re.match(r"^\[([^\]]+)\] (Начата обработка документа\.|100%: Готово)$", line)
        if match:
            when = datetime.fromisoformat(match[1])
            if match[2].startswith("Начата"):
                started = when
            elif started is not None:
                last = (when - started).total_seconds()
                started = None
    return last


def read_lines(path):
    if path.is_file():
        return path.read_text(encoding="utf-8").splitlines()
    return ()


def load_reference(source, job_dir):
    original_hash = sha256_file(source)
    originals = list((job_dir / "input").glob("*.pdf"))
    if len(originals) != 1 or sha256_file(originals[0]) != original_hash:
        raise ValueError(f"Вход «Точного» не совпадает с {source.name}: {job_dir}")
    result = job_dir / "result"
    canonical = result / ".replay" / "document.html"
    manifest = json.loads(canonical.with_name("manifest.json").read_text(encoding="utf-8"))
    if sha256_file(canonical) != manifest["html_sha256"].lower():
        raise ValueError(f"SHA-256 эталонного HTML не совпадает с манифестом: {canonical}")
    markdown = sorted(result.glob("*_md/*_converted.md"))
    if len(markdown) > 1:
        raise ValueError(f"Неоднозначный эталон Markdown: {job_dir}")
    metrics, text = document_metrics(canonical, markdown[0] if markdown else None)
    status = backend_active = None
    for line in read_lines(result / "engine.log"):
        if line.startswith("Status: "):
            status = line.removeprefix("Status: ").split(";", 1)[0].strip()
        elif line.startswith("LOCAL_ENGINE_HOST "):
            event = json.loads(line.removeprefix("LOCAL_ENGINE_HOST "))
            if event.get("event") == "job_started":
                backend_active = bool(event.get("backend_pid"))
    return dict(
        mode="quality", metrics=metrics, text=text, pages=manifest["stats"]["pages"],
        source_hash=original_hash, status=status,
        backend_active=backend_active, elapsed=historical_timing(read_lines(job_dir / "conversion.log")),
    )


def trace_pages(fast_pdf, pages_module, text_module, records):
    read_pages = fast_pdf.read_pages
    page_text = pages_module.page_text
    usable_text = text_module.usable_text
    covers_body = pages_module.covers_scanned_body
    current = {}

    def checked_text(*args, **kwargs):
        usable = usable_text(*args, **kwargs)
        current["usable"] = usable
        return usable

    def checked_layer(document, number, page_size):
        current.update(number=number, usable=None)
        reading = page_text(document, number, page_size)
        records[number] = dict(usable=current["usable"], layer=reading is not None, coverage=None)
        return reading

    def checked_coverage(*args, **kwargs):
        covered = covers_body(*args, **kwargs)
        records[current["number"]]["coverage"] = covered
        return covered

    def observed_pages(*args, **kwargs):
        with closing(read_pages(*args, **kwargs)) as pages:
            for number, page in pages:
                yield number, page
                record = records.setdefault(number, dict(usable=None, layer=None, coverage=None))
                method = "blank" if page.blank else ("native" if page.text_layer is not None else "ocr")
                record.update(
                    method=method, turn=page.turn, skew=(page.source_view or {}).get("deskew_degrees", 0),
                    source=page.source, grids=[(grid.rows, grid.columns, len(grid.cells)) for grid in page.grids],
                    reasons=list(dict.fromkeys(page.reasons)),
                )

    return (
        patch.object(text_module, "usable_text", checked_text),
        patch.object(pages_module, "page_text", checked_layer),
        patch.object(pages_module, "covers_scanned_body", checked_coverage),
        patch.object(fast_pdf, "read_pages", observed_pages),
    )


def export_result(source, scratch, content, stats, images):
    from local_web.converter import export_formats
    from local_web.export_bundle import build_exports

    started = perf_counter()
    html = export_formats(content, source, scratch, pages=stats["pages"], output_format="html",
                          initial_warnings=stats.get("warnings", ()), fast_pdf_stats=stats, fast_assets=images)
    times = {"html": perf_counter() - started}
    canonical = html["replay_html"]
    artifacts = dict(html["artifacts"])
    for kind in ("markdown", "json"):
        started = perf_counter()
        bundle = build_exports(None, source, scratch, output_format=kind, replay_html=canonical)
        times[kind] = perf_counter() - started
        artifacts.update(bundle["artifacts"])
    return canonical, artifacts, times


def convert_worker(request):
    import win32api
    import win32con

    suppress_dialog = win32con.SEM_NOGPFAULTERRORBOX
    win32api.SetErrorMode(win32api.SetErrorMode(suppress_dialog) | suppress_dialog)
    sys.path.insert(0, str(ROOT))
    from portable_runtime import configure_runtime

    source = Path(request["source"])
    scratch = Path(request["scratch"]).resolve(strict=True)
    if scratch.parent.parent != ROOT or not scratch.parent.name.startswith(".ocr-bench-"):
        raise ValueError(f"Некорректная временная папка: {scratch}")
    mode = request["mode"]
    os.environ.update(TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch), CUDA_CACHE_DISABLE="1")
    os.environ.pop("SURYA_INFERENCE_PORT", None)
    configure_runtime()
    from local_web import balanced_table_pages, fast_ocr, fast_pdf, fast_pdf_text

    manager = None
    diagnostics = Counter()
    rapid_providers = set()
    infer = fast_ocr._infer

    def observe_inference(image, word_boxes):
        result = infer(image, word_boxes)
        cuda = not fast_ocr._cuda_failed and bool(fast_ocr._cuda_libraries())
        ocr = fast_ocr._rapid_reader(cuda)
        for component in (ocr.text_det, ocr.text_rec):
            if component is not None:
                rapid_providers.add(component.session.session.get_providers()[0])
        return result

    def get_manager():
        nonlocal manager
        if manager is None:
            from local_web.engine_process import prepare_runtime

            manager = prepare_runtime()
        return manager

    def diagnostic(event, **fields):
        diagnostics[event] += 1
        if fields.get("error"):
            print(f"{event}: {fields['error']}", file=sys.stderr)

    if mode == "balanced":
        import docling_surya_profile
        from docling_pdf_balanced_math import local_formula_reader

        docling_surya_profile.surya25_cache_path = lambda config=None: scratch / "surya"

    runs = []
    stable = True
    pages = {}
    with ExitStack() as scope:
        scope.enter_context(patch.object(fast_ocr, "_infer", observe_inference))
        for observer in trace_pages(fast_pdf, balanced_table_pages, fast_pdf_text, pages):
            scope.enter_context(observer)
        try:
            for run in range(4):
                images = {}
                pages.clear()
                reader = local_formula_reader(get_manager, diagnostic=diagnostic) if mode == "balanced" else None
                started = request["kickoff"] if run == 0 else perf_counter()
                content, stats = fast_pdf.convert_fast_pdf(source, mode=mode, image_assets=images, formula_reader=reader)
                runs.append(perf_counter() - started)
                if run == 0:
                    baseline = content, stats, images
                    page_records = pages.copy()
                else:
                    stable &= content == baseline[0] and images.keys() == baseline[2].keys()
            backend = manager.backend.handle if manager is not None else None
        finally:
            if manager is not None:
                manager.stop()

    canonical, artifacts, export_seconds = export_result(source, scratch, *baseline)
    return dict(
        mode=mode, runs=runs, stable=stable, routes=page_records, stats=baseline[1],
        rapid=sorted(rapid_providers), surya=os.environ.get("TORCH_DEVICE") if backend else None,
        diagnostics=dict(diagnostics), canonical=canonical, artifacts=artifacts, export_seconds=export_seconds,
    )


def worker_entry():
    output = StringIO()
    try:
        request = json.load(sys.stdin)
        with redirect_stdout(output):
            result = convert_worker(request)
    except Exception:
        print("\n".join(output.getvalue().splitlines()[-12:]), file=sys.stderr)
        raise
    print(json.dumps(result))


def run_worker(source, mode, scratch):
    import win32api
    import win32con
    import win32job

    scratch.mkdir()
    command = [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--worker"]
    request = dict(source=str(source), mode=mode, scratch=str(scratch), kickoff=perf_counter())
    with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        job = win32job.CreateJobObject(None, "")
        try:
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            rights = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
            handle = win32api.OpenProcess(rights, False, process.pid)
            try:
                win32job.AssignProcessToJobObject(job, handle)
            finally:
                handle.Close()
            stdout, stderr = process.communicate(json.dumps(request), timeout=1500)
        finally:
            job.Close()
            if process.poll() is None:
                process.kill()
        if process.returncode != 0:
            tail = "\n".join(stderr.splitlines()[-18:])
            raise RuntimeError(f"{source.name}, {MODES[mode]}: процесс завершился с кодом {process.returncode}\n{tail}")
    result = json.loads(stdout)
    if result["diagnostics"]:
        details = [line for line in stderr.splitlines() if line.startswith("BALANCED_FORMULA_")]
        result["formula_errors"] = list(dict.fromkeys(details))[:2]
    return result


def yes_no(value):
    return {True: "да", False: "нет", None: "н/д"}[value]


def seconds(value):
    return f"{value:.3f}" if value is not None else "н/д"


def result_row(name, result):
    metrics = result["metrics"]
    runs = result.get("runs", ())
    exports = result.get("export_seconds")
    leaks = metrics["leaks"]
    leak_counts = f"{leaks['paragraph']} / {leaks['math']} / {leaks['total']}" if leaks else "н/д"
    formats = f"{metrics['mathml']} / {metrics['formula_text']} / {metrics['latex']} / {metrics['formula_png']}"
    return (
        f"| {name} | {MODES[result['mode']]} | {seconds(runs[0] if runs else None)} | "
        f"{seconds(median(runs[1:]) if runs else None)} | {seconds(result.get('elapsed'))} | "
        f"{' / '.join(seconds(exports[kind]) for kind in ('html', 'markdown', 'json')) if exports else 'н/д'} | "
        f"{metrics['tables']} / {metrics['cells']} | {metrics['images']} / {metrics['page_images']} / "
        f"{metrics['audit_images']} | {formats} | {metrics['linked_formula_crops']} | "
        f"{leak_counts} | {metrics['text_gap']:.2%} ({metrics['tokens']}) |"
    )


def page_description(record):
    grids = ", ".join(f"{rows}×{columns} ({cells})" for rows, columns, cells in record["grids"])
    reasons = ", ".join(record["reasons"])
    layout = " ".join(value for value in (record["source"], grids, reasons) if value) or "—"
    return (
        f"{record['method']} · {yes_no(record['usable'])}/{yes_no(record['layer'])}/{yes_no(record['coverage'])} · "
        f"{record['turn']}°/{record['skew']:+.2f}° · {layout}"
    )


def make_report(documents, base, fingerprint):
    packages = ", ".join(f"{name} {version(name)}" for name in
                         ("rapidocr", "onnxruntime-gpu", "opencv-python-headless", "surya-ocr", "docling", "torch"))
    lines = [
        "# Базовые метрики REGION OCR",
        "",
        f"Замеры выполнены в «REGION OCR — копия». Базовый коммит {base}. "
        f"SHA-256 рабочего дерева Python-модулей {fingerprint}.",
        f"Python {sys.version.split()[0]}. Установлены {packages}.",
        "",
        "## Сводка",
        "",
        "| PDF | Режим | Холодный, с | Тёплый, с | История, с | Экспорт HTML / MD / JSON, с | "
        "Таблицы / ячейки | Изображения / страницы / кропы сверки | MathML / текст / LaTeX / PNG | "
        "PNG при MathML | MD `<p>` / `<math>` / всего | Расхождение (токены) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source, results in documents:
        lines.extend(result_row(source.stem, item) for item in results)
    lines += [
        "",
        "Холодный замер включает запуск процесса. Тёплый — медиана трёх повторов того же PDF в этом процессе "
        "с действующими кешами OCR и формул. Экспорт измерен по первому результату. Время «Точного» взято "
        "между началом обработки и 100% готовности по журналу. Состояние кешей и отдельное время экспорта "
        "в исторических запусках неизвестны.",
        "",
        "Счётчики отражают элементы канонического HTML. Кроп при MathML — дополнительное изображение формулы. "
        "Утечки Markdown — строчные фрагменты кода с начальным `<p>` или тегом `<math>`. "
        "Расхождение рассчитано по видимому тексту всего документа с сохранением регистра и знаков. "
        "Оно включает различия порядка и структуры текста.",
        "",
        "## Маршруты страниц",
        "",
        "В каждой ячейке указаны маршрут, проверки `usable_text` / получения слоя / покрытия скана, "
        "поворот / поправка наклона и решение детектора. Размер сетки дан в строках × столбцах. В скобках число ячеек. "
        "У «Точного» есть собственная диагностика Surya и Docling.",
    ]
    for source, (fast, balanced, reference) in documents:
        lines += [
            "", f"### {source.name}", "",
            f"SHA-256 PDF {reference['source_hash']}. Эталон «Точного» имеет статус {reference['status'] or 'н/д'}. "
            f"Surya на старте задания — {'уже запущена' if reference['backend_active'] else 'н/д'}.",
            "",
        ]
        for item in (fast, balanced):
            model = item["surya"] or "не запускалась"
            rapid = ", ".join(item["rapid"]) or "не вызывался"
            warm = ", ".join(seconds(value) for value in item["runs"][1:])
            stable = "совпали" if item["stable"] else "различались"
            lines.append(f"- «{MODES[item['mode']]}». Провайдер RapidOCR — {rapid}. Surya — {model}. "
                         f"Тёплые повторы — {warm} с. HTML движка и набор ресурсов {stable}.")
            if item["diagnostics"]:
                summary = ", ".join(f"{event} ({count})" for event, count in sorted(item["diagnostics"].items()))
                lines.append(f"  События Surya — {summary}.")
                for detail in item.get("formula_errors", ()):
                    lines.append(f"  `{detail[:240].replace(chr(96), '')}`")
        lines += ["", "| Стр. | Быстрый | Сбалансированный |", "|---:|---|---|"]
        for number in range(1, reference["pages"] + 1):
            lines.append(f"| {number} | {page_description(fast['routes'][str(number)])} | "
                         f"{page_description(balanced['routes'][str(number)])} |")
    lines += [
        "", "Эталон ГОСТ 9327-60 содержит «¾ A7» вместо «⅛ A7». Для страницы 9 в журнале указано, "
        "что при сборке потеряны 3 из 67 ячеек. В ГОСТ 30494-2011 страницы 17 и 19, 18 и 20 "
        "совпадают в исходнике.", "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="Папка с файлами *_original.pdf")
    parser.add_argument("--reference", type=Path, nargs=2, action="append", required=True,
                        metavar=("PDF", "JOB_DIR"), help="Имя исходного PDF и папка задания «Точного»")
    args = parser.parse_args()
    if not (ROOT / "runtime" / "python.exe").samefile(sys.executable):
        parser.error("Запустите стенд через runtime\\python.exe из этой копии REGION OCR")
    sources = sorted(args.corpus.resolve().glob("*_original.pdf"))
    jobs = {name.name: path.resolve() for name, path in args.reference}
    if not sources or {source.name for source in sources} != jobs.keys() or len(jobs) != len(args.reference):
        parser.error("Для каждого *_original.pdf нужен ровно один --reference PDF JOB_DIR")
    references = [load_reference(source, jobs[source.name]) for source in sources]
    base, fingerprint = revision()

    documents = []
    with TemporaryDirectory(prefix=".ocr-bench-", dir=ROOT) as temp:
        scratch = Path(temp).resolve(strict=True)
        if scratch.parent != ROOT:
            raise RuntimeError(f"Временная папка вышла за пределы копии: {scratch}")
        for index, (source, reference) in enumerate(zip(sources, references, strict=True)):
            results = []
            for mode in ("fast", "balanced"):
                print(f"{source.name} · {MODES[mode]}", flush=True)
                result = run_worker(source, mode, scratch / f"{index}-{mode}")
                routes = result["routes"]
                if set(routes) != {str(number) for number in range(1, reference["pages"] + 1)}:
                    raise ValueError(f"Номера страниц {source.name} не совпадают с эталоном")
                result["metrics"], _ = document_metrics(
                    Path(result["canonical"]), Path(result["artifacts"]["markdown"]), reference["text"]
                )
                results.append(result)
                print(f"  {seconds(result['runs'][0])} с холодный, "
                      f"{seconds(median(result['runs'][1:]))} с тёплый", flush=True)
            documents.append((source, (*results, reference)))

    if revision() != (base, fingerprint):
        raise RuntimeError("Код приложения изменился во время замеров")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(make_report(documents, base, fingerprint), encoding="utf-8")
    print(REPORT)


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        worker_entry()
    else:
        main()
