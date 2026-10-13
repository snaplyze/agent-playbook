#!/usr/bin/env python3
"""Сборка страницы GitHub Pages из prompts/ и таблиц README.

Запуск: python3 scripts/build_site.py --out _site
"""
import argparse
import html
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "snaplyze/agent-playbook"
BEGIN, END = "<!-- BEGIN agent-contract -->", "<!-- END agent-contract -->"


def fail(message):
    sys.exit(f"build_site: {message}")


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def render(lines, level=3):
    """Подмножество Markdown, которым написаны промпты: заголовки, абзацы, списки, блок кода."""
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith("<!--"):
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            body = lines[i + 1:j]
            if line[3:].strip() == "markdown":
                out.append(f'<div class="attachment">{render(body, level + 1)}</div>')
            else:
                out.append(f"<pre><code>{html.escape(chr(10).join(body), quote=False)}</code></pre>")
            i = j + 1
        elif match := re.match(r"(#{1,3}) (.*)", line):
            tag = f"h{min(level + len(match.group(1)) - 1, 6)}"
            out.append(f"<{tag}>{inline(match.group(2))}</{tag}>")
            i += 1
        elif re.match(r"(\d+\.|-) ", line):
            block, i = render_list(lines, i, level)
            out.append(block)
        else:
            j = i + 1
            while j < len(lines) and lines[j].strip() and not re.match(r"(#{1,3} |```|<!--|(\d+\.|-) )", lines[j]):
                j += 1
            out.append(f"<p>{inline(' '.join(part.strip() for part in lines[i:j]))}</p>")
            i = j
    return "\n".join(out)


def render_list(lines, i, level):
    ordered = bool(re.match(r"\d+\. ", lines[i]))
    marker = r"(\d+)\. (.*)" if ordered else r"()- (.*)"
    start = int(re.match(r"\d+", lines[i]).group(0)) if ordered else 1
    items = []
    while i < len(lines) and (match := re.match(marker, lines[i])):
        body, i = [match.group(2)], i + 1
        while i < len(lines):
            if lines[i].startswith("   "):
                body.append(lines[i][3:])
            elif not lines[i].strip() and i + 1 < len(lines) and lines[i + 1].startswith("   "):
                body.append("")
            else:
                break
            i += 1
        items.append(inline(body[0]) if len(body) == 1 else render(body, level))
    tag = "ol" if ordered else "ul"
    attr = f' start="{start}"' if ordered and start != 1 else ""
    return f"<{tag}{attr}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{tag}>", i


def scenarios_from_readme():
    """Сценарии и параметры берутся из таблиц README — того же источника, что видят люди на GitHub."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    scenarios = {}
    row = r"^\| (\d\d) \| \[([^\]]+)\]\((prompts/[^)]+)\) \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \|$"
    for num, name, path, when, result, never in re.findall(row, text, re.M):
        scenarios[num] = dict(num=num, name=name, path=path, when=when, result=result, never=never, params=[])
    for num, param, note in re.findall(r"^\| (\d\d) \| `([^`]+)` \| ([^|]+?) \|$", text, re.M):
        if num not in scenarios:
            fail(f"README: параметр {param} у неизвестного сценария {num}")
        scenarios[num]["params"].append((param, note))
    files = {f"prompts/{path.name}" for path in (ROOT / "prompts").glob("*.md")}
    listed = {item["path"] for item in scenarios.values()}
    if files != listed:
        fail(f"README и prompts/ расходятся: {sorted(files ^ listed)}")
    return [scenarios[num] for num in sorted(scenarios)]


# Как показать параметр на странице. Ключ — имя параметра из таблицы README.
# select: (подпись, значение, подсказка поля) — значение None означает «по умолчанию, строки нет»,
# подсказка не None — вариант просит дописать значение вручную.
# help — пояснение под элементом: варианты уже видны в нем, поэтому здесь только смысл.
PARAM_UI = {
    "CI": {"kind": "select", "help": "Решение владельца важнее обнаруженных раннеров. Код из недоверенных источников на self-hosted не попадает в любом случае.", "options": [
        ("По обнаружению", None, None),
        ("Только hosted", "только hosted", None),
        ("Self-hosted для всех подходящих заданий", "self-hosted для всех подходящих заданий", None),
        ("Свое правило…", "", "например: self-hosted Linux x64 для сборки, остальное — hosted"),
    ]},
    "Доступ": {"kind": "check", "help": "Широкий неинтерактивный доступ агента на уровне проекта. Это ваш выбор, а не безопасный стандарт."},
    "Установить оркестрацию": {"kind": "text", "placeholder": "репозиторий или путь установщика",
                               "help": "Откуда установить роли или расширение для подагентов и в какую область. Без источника агент только проверяет, что уже настроено.",
                               "scope": ["проект", "пользователь"]},
    "Объем": {"kind": "select", "help": "По умолчанию агент доводит все согласованные задачи плана.", "options": [
        ("Весь план", None, None),
        ("Аудит…", "аудит", "название или дата аудита"),
        ("Список ID…", "", "например: A-03, A-07"),
    ]},
    "Разрешено": {"kind": "multi", "help": "Неотмеченные действия агент только подготовит и предложит вам.", "options": [
        "создание репозитория", "создание основной ветки", "первичная загрузка", "смена видимости",
        "смена доступов", "другие ветки и теги", "перенос wiki", "перенос задач", "перенос релизов", "деплой",
    ]},
}


def param_field(slug, index, param, note, raw):
    key = param.partition(": ")[0]
    ui = PARAM_UI.get(key) or fail(f"README: для параметра «{key}» нет описания в PARAM_UI")
    # Варианты на странице должны дословно встречаться в тексте сценария, иначе агент их не узнает.
    words = [value for _, value, _ in ui.get("options", []) if value] if ui["kind"] == "select" else []
    words += [f"`{value}`" for value in ui.get("options", [])] if ui["kind"] == "multi" else []
    words += ui.get("scope", [])
    for word in words:
        if word not in raw:
            fail(f"вариант «{word}» параметра «{key}» не встречается в тексте сценария")
    fid = f"{slug}-p{index}"
    esc = html.escape
    help_html = f'<span class="help" id="{fid}-help">{inline(ui.get("help", note))}</span>'
    if ui["kind"] == "check":
        return (f'<div class="field check" data-kind="check" data-line="{esc(param)}">'
                f'<input id="{fid}" type="checkbox" aria-describedby="{fid}-help">'
                f'<label for="{fid}">{esc(param)}</label>{help_html}</div>')
    if ui["kind"] == "multi":
        choices = "".join(f'<label class="choice"><input type="checkbox" value="{esc(value)}"> {esc(value)}</label>'
                          for value in ui["options"])
        return (f'<div class="field wide" data-kind="multi" data-key="{esc(key)}" role="group" '
                f'aria-labelledby="{fid}-label" aria-describedby="{fid}-help">'
                f'<span class="label" id="{fid}-label">{esc(key)}</span><div class="choices">{choices}</div>{help_html}</div>')
    if ui["kind"] == "select":
        options = "".join(
            f'<option value="{esc(value or "")}"' + (f' data-input="{esc(hint)}"' if hint is not None else "")
            + f">{esc(label)}</option>" for label, value, hint in ui["options"])
        return (f'<div class="field" data-kind="select" data-key="{esc(key)}">'
                f'<label for="{fid}">{esc(key)}</label>'
                f'<select id="{fid}" aria-describedby="{fid}-help">{options}</select>'
                f'<input type="text" hidden autocomplete="off" spellcheck="false" aria-label="{esc(key)}: значение">'
                f'{help_html}</div>')
    scope = "".join(f"<option>{esc(value)}</option>" for value in ui["scope"])
    return (f'<div class="field" data-kind="text" data-key="{esc(key)}">'
            f'<label for="{fid}">{esc(key)}</label><div class="combo">'
            f'<input id="{fid}" type="text" autocomplete="off" spellcheck="false" placeholder="{esc(ui["placeholder"])}" '
            f'aria-describedby="{fid}-help"><select aria-label="Область установки">{scope}</select></div>{help_html}</div>')


def scenario_section(item):
    raw = (ROOT / item["path"]).read_text(encoding="utf-8")
    slug = Path(item["path"]).stem.split("-", 1)[1]
    item["slug"] = slug
    fields = "".join(param_field(slug, i, param, note, raw) for i, (param, note) in enumerate(item["params"]))
    params = f'<fieldset class="params"><legend>Параметры</legend>{fields}</fieldset>' if fields else ""
    block_button = ('<button type="button" class="secondary" data-copy="block">Скопировать блок для AGENTS.md</button>'
                    if BEGIN in raw else "")
    size = f"{len(raw):,}".replace(",", "\u00a0")
    return f"""
<section class="scenario" id="{slug}" aria-labelledby="{slug}-title">
  <h2 id="{slug}-title"><span class="num">{item["num"]}</span> {html.escape(item["name"])}</h2>
  <dl class="facts">
    <div><dt>Когда</dt><dd>{inline(item["when"])}</dd></div>
    <div><dt>Результат</dt><dd>{inline(item["result"])}</dd></div>
    <div><dt>Чего не делает</dt><dd>{inline(item["never"])}</dd></div>
  </dl>
  {params}
  <div class="actions needs-js">
    <button type="button" class="primary" data-copy="prompt">Скопировать поручение</button>
    {block_button}
    <button type="button" class="quiet" data-toggle aria-pressed="false">Исходный текст</button>
    <span class="stamp" aria-hidden="true"></span>
  </div>
  <p class="source">Файл <a href="https://github.com/{REPO}/blob/main/{item["path"]}">{item["path"]}</a>, {size} символов. Параметры добавляются строками перед текстом.</p>
  <article class="sheet">
    <pre class="fill" hidden></pre>
    <div class="doc">{render(raw.splitlines())}</div>
    <pre class="raw">{html.escape(raw, quote=False)}</pre>
  </article>
</section>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="_site", help="каталог для index.html")
    out = Path(parser.parse_args().out)
    items = scenarios_from_readme()
    sections = "".join(scenario_section(item) for item in items)
    rail = "".join(f'<li><a href="#{item["slug"]}" data-id="{item["slug"]}"><span class="num">{item["num"]}</span>'
                   f'<span class="name">{html.escape(item["name"])}</span></a></li>' for item in items)
    sha = os.environ.get("GITHUB_SHA", "")[:7]
    built = f' Собрано из коммита <a href="https://github.com/{REPO}/commit/{sha}">{sha}</a>.' if sha else ""
    page = (TEMPLATE.replace("@RAIL@", rail).replace("@SECTIONS@", sections)
            .replace("@REPO@", REPO).replace("@BUILT@", built))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{out / 'index.html'}: {len(items)} сценариев, {len(page.encode())} байт")


TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-playbook</title>
<meta name="description" content="Поручения для AI-агентов, которые работают с кодом: пять сценариев и общий контракт для AGENTS.md, не привязанные к конкретному клиенту.">
<meta property="og:title" content="agent-playbook">
<meta property="og:description" content="Пять сценариев и общий контракт для AGENTS.md: выберите поручение, задайте параметры и скопируйте текст.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect x='3' y='3' width='26' height='26' rx='5' fill='none' stroke='%235B3FA8' stroke-width='3'/%3E%3Crect x='9' y='9' width='14' height='14' rx='2' fill='%235B3FA8'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600&family=PT+Serif:ital,wght@0,400;0,700;1,400&display=swap">
<style>
:root {
  --paper: #F2F4F3;
  --sheet: #FFFFFF;
  --ink: #1B2333;
  --ink-soft: #4E5767;
  --rule: #D3D8DD;
  --stamp: #5B3FA8;
  --stamp-wash: #EEE9F9;
  --on-ink: #FFFFFF;
  --serif: "PT Serif", Georgia, "Times New Roman", serif;
  --sans: "Golos Text", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #11151D;
    --sheet: #181D27;
    --ink: #E4E7EC;
    --ink-soft: #A0A8B6;
    --rule: #2B3240;
    --stamp: #B39CFF;
    --stamp-wash: #262140;
    --on-ink: #11151D;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --paper: #11151D; --sheet: #181D27; --ink: #E4E7EC; --ink-soft: #A0A8B6;
  --rule: #2B3240; --stamp: #B39CFF; --stamp-wash: #262140; --on-ink: #11151D;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 400 16px/1.5 var(--sans);
}
a { color: inherit; text-decoration-color: var(--stamp); text-underline-offset: 3px; }
a:hover { color: var(--stamp); }
:focus-visible { outline: 2px solid var(--stamp); outline-offset: 2px; }
code { -webkit-box-decoration-break: clone; box-decoration-break: clone; font: 0.86em/1.4 var(--mono); background: var(--paper); border: 1px solid var(--rule); border-radius: 3px; padding: 0 0.25em; }

.wrap { max-width: 1160px; margin: 0 auto; padding: 0 16px; }
.top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 4px 16px; padding: 20px 0 0; font-size: 15px; color: var(--ink-soft); }
.top strong, .top a { white-space: nowrap; }
.top strong { color: var(--ink); font-weight: 600; }
.intro { padding: 48px 0 40px; max-width: 46rem; }
.intro h1 { font: 700 clamp(2rem, 5vw, 3.1rem)/1.08 var(--serif); letter-spacing: -0.015em; margin: 0 0 20px; text-wrap: balance; }
.intro p { font-size: 1.125rem; line-height: 1.55; color: var(--ink-soft); margin: 0; max-width: 40rem; }

.layout { display: grid; grid-template-columns: clamp(176px, 17vw, 210px) minmax(0, 1fr); gap: clamp(28px, 4vw, 48px); align-items: start; padding-bottom: 64px; }
.rail { position: sticky; top: 24px; }
.rail ul { list-style: none; margin: 0; padding: 0; border-left: 1px solid var(--rule); }
.rail a { display: flex; gap: 12px; align-items: baseline; min-height: 44px; padding: 10px 0 10px 16px; margin-left: -1px; border-left: 3px solid transparent; text-decoration: none; color: var(--ink-soft); }
.rail a:hover { color: var(--ink); }
.rail a[aria-current="page"] { color: var(--ink); border-left-color: var(--stamp); font-weight: 500; }
.rail .num, h2 .num { font-variant-numeric: tabular-nums; color: var(--stamp); font-weight: 500; }
.rail .order { margin: 20px 0 0; padding-left: 16px; font-size: 14px; line-height: 1.5; color: var(--ink-soft); }

.scenario { scroll-margin-top: 24px; }
.scenario + .scenario { margin-top: 72px; }
.js .scenario + .scenario { margin-top: 0; }
.scenario h2 { font: 700 2rem/1.15 var(--serif); margin: 0 0 20px; letter-spacing: -0.01em; }
.scenario h2 .num { font: 500 1.1rem/1 var(--sans); margin-right: 6px; vertical-align: 0.35em; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 16px 28px; margin: 0 0 28px; max-width: 60rem; }
.facts dt { font-size: 14px; color: var(--ink-soft); margin-bottom: 4px; }
.facts dd { margin: 0; line-height: 1.45; }

.params { border: 0; padding: 0; margin: 0 0 24px; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 16px 24px; max-width: 60rem; }
.params legend { font: 700 1.05rem/1.3 var(--serif); padding: 0; margin-bottom: 12px; }
.field { display: grid; gap: 6px; align-content: start; }
.field label { font-weight: 500; }
.field input[type="text"] { font: 400 16px/1.4 var(--mono); color: var(--ink); background: var(--sheet); border: 1px solid var(--rule); border-radius: 6px; padding: 10px 12px; min-height: 44px; width: 100%; }
.field select { font: 400 16px/1.4 var(--sans); color: var(--ink); background: var(--sheet); border: 1px solid var(--rule); border-radius: 6px; padding: 10px 12px; min-height: 44px; width: 100%; max-width: 100%; }
.field input[type="text"]:focus, .field select:focus { border-color: var(--stamp); outline: 2px solid var(--stamp-wash); outline-offset: 0; }
.field input[type="text"][hidden] { display: none; }
.field .label { font-weight: 500; }
.field.wide { grid-column: 1 / -1; }
.choices { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 130px), 1fr)); gap: 2px 12px; }
.choice { display: flex; gap: 10px; align-items: center; min-height: 40px; line-height: 1.3; cursor: pointer; }
.choice input { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--stamp); }
.combo { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
.combo select { width: auto; }
html:not(.js) .params { display: none; }
.field.check { grid-template-columns: auto 1fr; column-gap: 10px; }
.field.check input { width: 18px; height: 18px; margin: 3px 0 0; accent-color: var(--stamp); }
.field.check .help { grid-column: 2; }
.help { font-size: 14px; line-height: 1.45; color: var(--ink-soft); }

.actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; position: relative; margin-bottom: 12px; }
button { font: 500 15px/1.2 var(--sans); border-radius: 6px; padding: 11px 16px; min-height: 44px; cursor: pointer; border: 1px solid var(--ink); }
button.primary { background: var(--ink); color: var(--on-ink); }
button.primary:hover { background: var(--stamp); border-color: var(--stamp); }
button.secondary { background: transparent; color: var(--ink); }
button.secondary:hover { border-color: var(--stamp); color: var(--stamp); }
button.quiet { background: transparent; color: var(--ink-soft); border-color: transparent; padding-left: 6px; padding-right: 6px; text-decoration: underline; text-decoration-color: var(--rule); text-underline-offset: 4px; }
button.quiet:hover { color: var(--ink); text-decoration-color: var(--stamp); }
.stamp {
  position: absolute; z-index: 1; white-space: nowrap;
  pointer-events: none; padding: 4px 12px;
  font: 600 15px/1.2 var(--sans); color: var(--stamp); background: var(--paper);
  border: 3px double var(--stamp); border-radius: 5px;
  transform: rotate(-5deg); opacity: 0;
}
.stamp.on { animation: stamp 2.2s ease-out forwards; }
@keyframes stamp {
  0% { opacity: 0; transform: rotate(-5deg) scale(1.5); }
  10% { opacity: 1; transform: rotate(-5deg) scale(1); }
  80% { opacity: 1; transform: rotate(-5deg) scale(1); }
  100% { opacity: 0; transform: rotate(-5deg) scale(1); }
}
.source { font-size: 14px; color: var(--ink-soft); margin: 0 0 16px; }
.needs-js { display: none; }
.js .needs-js { display: flex; }

.sheet { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: clamp(20px, 4.5vw, 56px); }
.fill { font: 500 14px/1.6 var(--mono); color: var(--stamp); background: var(--stamp-wash); border-left: 3px solid var(--stamp); margin: 0 0 28px; padding: 10px 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.raw { display: none; margin: 0; font: 400 13.5px/1.6 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.show-raw .raw { display: block; }
.show-raw .doc { display: none; }

.doc { font: 400 1.0625rem/1.65 var(--serif); max-width: 42rem; }
.doc h3 { font: 700 1.6rem/1.2 var(--serif); margin: 0 0 18px; letter-spacing: -0.01em; }
.doc h4 { font: 700 1.2rem/1.3 var(--serif); margin: 36px 0 10px; }
.doc h5 { font: 600 1rem/1.35 var(--sans); margin: 28px 0 8px; }
.doc p { margin: 0 0 14px; }
.doc ul, .doc ol { margin: 0 0 16px; padding-left: 1.4em; }
.doc li { margin: 0 0 8px; padding-left: 0.2em; }
.doc li::marker { color: var(--ink-soft); font-family: var(--sans); font-size: 0.9em; }
.doc li > p { margin-bottom: 8px; }
.doc strong { font-weight: 700; }
.attachment { margin: 20px 0 0; padding: clamp(16px, 3.5vw, 36px); background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; font-size: 1rem; }
.attachment h4:first-child { margin-top: 0; }

footer { border-top: 1px solid var(--rule); padding: 24px 0 40px; font-size: 14px; color: var(--ink-soft); }
footer p { margin: 0; max-width: 46rem; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

@media (max-width: 860px) {
  .intro { padding: 32px 0 28px; }
  .layout { grid-template-columns: minmax(0, 1fr); gap: 32px; }
  .rail { position: static; }
  .rail ul { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; border-left: 0; }
  .rail a { flex-direction: column; align-items: flex-start; gap: 2px; height: 100%; margin: 0; padding: 8px 12px 10px; border: 1px solid var(--rule); border-radius: 6px; background: var(--sheet); font-size: 15px; line-height: 1.3; }
  .rail .num { font-size: 13px; }
  .rail a[aria-current="page"] { border-color: var(--stamp); box-shadow: inset 0 0 0 1px var(--stamp); }
  .rail .order { padding-left: 0; margin-top: 12px; }
  .scenario h2 { font-size: 1.65rem; }
}
@media (max-width: 739px) {
  .rail ul { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 520px) {
  .top { padding-top: 16px; }
  .intro { padding: 24px 0 20px; }
  .intro h1 { font-size: clamp(1.75rem, 8.5vw, 2.25rem); margin-bottom: 14px; }
  .intro p { font-size: 1rem; }
  .layout { gap: 28px; }
  .rail ul { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rail li:last-child:nth-child(odd) { grid-column: 1 / -1; }
  .scenario h2 { font-size: 1.5rem; }
  .actions button { flex: 1 1 auto; }
  .combo { grid-template-columns: minmax(0, 1fr); }
  .combo select { width: 100%; }
  .choice { font-size: 15px; }
  .actions button.quiet { flex: 0 0 auto; }
  .doc { font-size: 1rem; line-height: 1.6; }
  .doc h3 { font-size: 1.35rem; }
  .doc ul, .doc ol { padding-left: 1.2em; }
}
@media (prefers-reduced-motion: reduce) {
  .stamp.on { animation: none; opacity: 1; }
}
</style>
</head>
<body>
<div class="wrap">
  <div class="top"><strong>agent-playbook</strong><a href="https://github.com/@REPO@">Репозиторий на GitHub</a></div>
  <header class="intro">
    <h1>Поручения для агентов, которые работают с кодом</h1>
    <p>Пять сценариев и общий контракт для AGENTS.md. Тексты не привязаны к конкретному клиенту: выберите сценарий, задайте параметры, если нужно, и отправьте агенту весь текст одним сообщением.</p>
  </header>
  <div class="layout">
    <nav class="rail" aria-label="Сценарии">
      <ul>@RAIL@</ul>
      <p class="order">Обычно 00 запускают один раз в проекте, затем 01 и 03. 02 — когда разросся план, 04 — когда нужно доставить весь согласованный состав.</p>
    </nav>
    <main>@SECTIONS@</main>
  </div>
  <footer><p>Тексты собраны из каталога prompts/ репозитория <a href="https://github.com/@REPO@">@REPO@</a>, лицензия Apache-2.0.@BUILT@</p></footer>
</div>
<p class="sr-only" role="status" id="status"></p>
<script>
(() => {
  const BEGIN = "<!-- BEGIN agent-contract -->", END = "<!-- END agent-contract -->";
  document.documentElement.classList.add("js");
  const status = document.getElementById("status");
  const sections = [...document.querySelectorAll(".scenario")];
  const links = [...document.querySelectorAll(".rail a[data-id]")];

  function show(id, scroll) {
    sections.forEach((section) => { section.hidden = section.id !== id; });
    links.forEach((link) => {
      if (link.dataset.id !== id) return link.removeAttribute("aria-current");
      link.setAttribute("aria-current", "page");
    });
    const section = document.getElementById(id);
    if (scroll && section.getBoundingClientRect().top < 0) section.scrollIntoView({ block: "start" });
  }
  const current = () => sections.some((s) => s.id === location.hash.slice(1)) ? location.hash.slice(1) : sections[0].id;
  links.forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    history.replaceState(null, "", "#" + link.dataset.id);
    show(link.dataset.id, true);
  }));
  window.addEventListener("hashchange", () => show(current(), true));
  show(current(), false);
  // Браузер прокрутил к якорю до того, как скрипт спрятал остальные сценарии: выравниваем заново.
  if (location.hash) document.getElementById(current()).scrollIntoView({ block: "start" });

  function mark(button, message) {
    const stamp = button.parentElement.querySelector(".stamp");
    stamp.textContent = message;
    const room = button.parentElement.clientWidth - stamp.offsetWidth;
    stamp.style.left = Math.max(0, Math.min(button.offsetLeft + button.offsetWidth - stamp.offsetWidth * 0.6, room)) + "px";
    stamp.style.top = Math.max(0, button.offsetTop - stamp.offsetHeight * 0.55) + "px";
    stamp.classList.remove("on");
    void stamp.offsetWidth;
    stamp.classList.add("on");
    status.textContent = message;
  }

  sections.forEach((section) => {
    const raw = section.querySelector("pre.raw").textContent;
    const fill = section.querySelector(".fill");
    const fields = [...section.querySelectorAll(".field[data-kind]")];
    const toggle = section.querySelector("[data-toggle]");
    const clean = (value) => value.trim().replace(/\\s+/g, " ");
    // Строка параметра в том виде, в каком ее ждет сценарий; null — параметр не задан.
    function line(field) {
      const key = field.dataset.key;
      switch (field.dataset.kind) {
        case "check":
          return field.querySelector("input").checked ? field.dataset.line : null;
        case "multi": {
          const picked = [...field.querySelectorAll("input:checked")].map((input) => input.value);
          return picked.length ? key + ": " + picked.join(", ") : null;
        }
        case "select": {
          const option = field.querySelector("select").selectedOptions[0];
          const text = field.querySelector("input[type=text]");
          const needsText = "input" in option.dataset;
          text.hidden = !needsText;
          if (needsText) text.placeholder = option.dataset.input;
          const extra = needsText ? clean(text.value) : "";
          if (needsText ? !extra : !option.value) return null;
          return key + ": " + [option.value, extra].filter(Boolean).join(" ");
        }
        default: {
          const value = clean(field.querySelector("input").value);
          return value ? key + ": " + value + ", область: " + field.querySelector("select").value : null;
        }
      }
    }
    const lines = () => fields.map(line).filter(Boolean);
    const update = () => {
      const filled = lines();
      fill.textContent = filled.join("\\n");
      fill.hidden = filled.length === 0;
    };
    section.addEventListener("input", update);
    section.addEventListener("change", (event) => {
      update();
      const text = event.target.matches("select") && event.target.parentElement.querySelector("input[type=text]:not([hidden])");
      if (text && event.target.closest('[data-kind="select"]')) text.focus();
    });
    update();

    function setRaw(on) {
      section.classList.toggle("show-raw", on);
      toggle.setAttribute("aria-pressed", String(on));
      toggle.textContent = on ? "Оформленный текст" : "Исходный текст";
    }
    toggle.addEventListener("click", () => setRaw(!section.classList.contains("show-raw")));

    async function copy(text, button) {
      try {
        await navigator.clipboard.writeText(text);
        mark(button, "Скопировано");
      } catch {
        setRaw(true);
        const range = document.createRange();
        range.selectNodeContents(section.querySelector("pre.raw"));
        const selection = getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        mark(button, "Текст выделен, нажмите Ctrl+C");
      }
    }
    section.querySelector('[data-copy="prompt"]').addEventListener("click", (event) => {
      const filled = lines();
      copy((filled.length ? filled.join("\\n") + "\\n\\n" : "") + raw, event.currentTarget);
    });
    const block = section.querySelector('[data-copy="block"]');
    if (block) block.addEventListener("click", (event) => {
      copy(raw.slice(raw.indexOf(BEGIN), raw.indexOf(END) + END.length) + "\\n", event.currentTarget);
    });
  });
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
