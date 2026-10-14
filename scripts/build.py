#!/usr/bin/env python3
"""Сборка самодостаточных промптов, таблиц README и страницы GitHub Pages из src/.

  python3 scripts/build.py                собрать prompts/ и таблицы README
  python3 scripts/build.py --check        проверить, что все собрано из src/, и прогнать проверки; ничего не пишет
  python3 scripts/build.py --site _site   дополнительно собрать страницу в каталог _site

Полная проверка, как в CI (<tmp> — каталог вне репозитория): --check --site <tmp>/site --parity-cases <tmp>/cases.json,
затем node scripts/parity.mjs <tmp>/site/index.html <tmp>/cases.json.
"""
import sys

# Вывод всегда в UTF-8, непредставимое — экранируется: в локали C кириллица иначе роняет print (UnicodeEncodeError).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
if sys.version_info < (3, 11):  # до импорта: tomllib появился в 3.11
    sys.exit(f"build: нужен Python 3.11 или новее (модуль tomllib), запущен {sys.version.split()[0]}")

import argparse
import functools
import html
import itertools
import json
import math
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PROMPTS = ROOT / "prompts"
README = ROOT / "README.md"
PAGE = ROOT / "scripts" / "page"  # исходники страницы: разметка с метками, стили, логика сборки текста, интерфейс
REPO = "snaplyze/agent-playbook"
PARAMS, RULES = "{{ПАРАМЕТРЫ}}", "{{ПРАВИЛА}}"
PROMPT_LIMIT = 20000  # символов: промпт отправляется одним сообщением
# Пустое обязательное значение: агент должен спросить, а не взять пример из подсказки поля.
MISSING = "<не указано: спроси владельца, не угадывай>"
# Пробельные символы ввода: Юникодный White_Space, разделители C0 (\x1c-\x1f) и U+FEFF (его считает пробелом JS).
# Тело класса символов, одно и то же для re и для RegExp страницы: render_site подставляет его в скрипт
# страницы на место @WHITESPACE@, поэтому clean здесь и clean там не расходятся.
WHITESPACE = r"\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
SPACES = re.compile(f"[{WHITESPACE}]+")

# Промпты описывают возможности по сути: без названий клиентов, расширений, моделей и версий.
# Проверяется только src/: промпты и страница собраны из него. Список неполон по природе — это страховка, а не
# замена внимания. Решения по спорным случаям:
#  - кириллицей запрещены только формы, не совпадающие с обычными словами («Клод», «Копилот», «ЧатГПТ»);
#    «курсор», «кодекс», «сонет», «опус», «лама» — обычные слова, их названия клиентов и моделей латиницей ловятся;
#  - «Zed» запрещен только с заглавной буквы (редактор); «zed» строчными — не отдельное слово русского текста,
#    но и не имя клиента в тексте промпта, поэтому не запрещен;
#  - «$goal», «$skill-name» (строчные, как вызов навыка) запрещены, переменные окружения («$HOME», «$RUNNER_TEMP»)
#    и числа с «$» — нет; строчные переменные оболочки в тексте называй заглавными или пиши <tmp>;
#  - номер версии «1.2.3» запрещен, а четыре числа через точку («10.0.0.1») — нет: так записан IPv4-адрес
#    (версия из четырех частей тоже пройдет); дата с годом из четырех цифр («14.10.2026») не совпадает.
FORBIDDEN = [re.compile(pattern, re.I) for pattern in (
    r"\b(?:codex|claude|cursor|gemini|copilot|windsurf|aider|cline|roo|jules|openai|anthropic|chatgpt|deepseek"
    r"|mistral|qwen|grok|llama|sonnet|opus|haiku|jetbrains|intellij|vscode|plugins?)\b",
    r"\bgpt", r"(?-i:\bZed\b)", r"\bvs[\s-]?code\b", r"\bvisual studio\b",
    r"\b(?:клод|копайлот|копилот|джемини|виндсерф|антропик|опен\s?эй\s?ай|чат[\s-]?gpt"
    r"|чат[\s-]?джи[\s-]?пи[\s-]?ти|джи[\s-]?пи[\s-]?ти)\w*",
    r"\borchestration:", r"(?-i:\$[a-z][a-z0-9-]*\b)", r"create_goal", r"(?<![\w/-])/goal\b",
    r"policy-version", r"unified-agent-policy", r"\bv\d+\b",
    r"(?<![\d.])\d+\.\d+\.\d{1,3}\b(?!\.\d)",  # номер версии
    r"редакци\w*\s+\d", r"плагин",
)]
# Проверка самого списка: (текст, должен ли он быть запрещен).
FORBIDDEN_SAMPLES = [
    ("Клод", True), ("у Клода", True), ("ChatGPT", True), ("GPT-4o", True), ("чат-джи-пи-ти", True),
    ("Sonnet", True), ("Opus", True), ("Llama", True), ("VS Code", True), ("plugin", True), ("плагин", True),
    ("Cursor", True), ("CLAUDE.md", True), ("Zed", True), ("`$goal`", True), ("версия 3.11.4", True), ("v2", True),
    ("$HOME/x", False), ("$RUNNER_TEMP", False), ("10.0.0.1", False), ("192.168.1.10.", False),
    ("14.10.2026", False), ("курсор", False), ("кодекс", False), ("zed", False), ("organized", False),
    ("Python 3.11+", False),
]


def fail(message):
    sys.exit(f"build: {message}")


def read(path):
    """Текст файла в UTF-8; ошибка чтения — сообщение, а не трассировка."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"{path.relative_to(ROOT)}: файл не в UTF-8")
    except OSError as error:
        fail(f"{path.relative_to(ROOT)}: {error.strerror or error}")


# --- Модель: сценарии, параметры, сборка текста -------------------------------------------------

# Схема scenarios.toml. Белый список ключей: новое поле добавляй сюда вместе с кодом, который его читает.
SCENARIO_KEYS = {"file", "name", "when", "result", "never", "param"}
OPTION_PARAM_KEYS = {"key", "help", "option"}
CHOICES_PARAM_KEYS = {"key", "help", "choices", "default", "all", "none", "some"}
OPTION_KEYS = {"label", "line", "input", "choice"}
FILE_NAME = re.compile(r"(\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md")


def check_keys(table, allowed, where):
    extra = sorted(set(table) - allowed)
    if extra:
        fail(f"{where}: неизвестные ключи {', '.join(f'«{key}»' for key in extra)}; "
             f"допустимы {', '.join(f'«{key}»' for key in sorted(allowed))}")


def string_field(table, key, where):
    if key not in table:
        fail(f"{where}: нет поля «{key}»")
    if not isinstance(table[key], str) or not table[key].strip():
        fail(f"{where}: поле «{key}» должно быть непустой строкой")
    return table[key]


def string_list(table, key, where):
    values = table.get(key)
    if key not in table:
        fail(f"{where}: нет поля «{key}»")
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
        fail(f"{where}: поле «{key}» должно быть непустым списком непустых строк")
    repeated = sorted({v for v in values if values.count(v) > 1})
    if repeated:
        fail(f"{where}: в поле «{key}» повторяются {', '.join(f'«{v}»' for v in repeated)}")
    return values


def table_list(parent, key, where):
    items = parent.get(key, [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        fail(f"{where}: «{key}» должно быть массивом таблиц [[...]]")
    return items


def validate_param(where, param):
    kinds = [kind for kind in ("option", "choices") if kind in param]
    if len(kinds) != 1:
        fail(f"{where}: нужно ровно одно из «option» (выбор одного) и «choices» (выбор нескольких)")
    check_keys(param, CHOICES_PARAM_KEYS if kinds[0] == "choices" else OPTION_PARAM_KEYS, where)
    string_field(param, "help", where)
    if kinds[0] == "choices":
        string_list(param, "choices", where)
        if param.get("default") not in ("all", "none"):
            fail(f"{where}: поле «default» должно быть «all» или «none»")
        for key in ("all", "none", "some"):
            string_field(param, key, where)
        return
    options = table_list(param, "option", where)
    if not options:
        fail(f"{where}: нет вариантов")
    for number, option in enumerate(options, 1):
        label = option.get("label")
        place = f"{where}, вариант «{label}»" if isinstance(label, str) else f"{where}, вариант №{number}"
        check_keys(option, OPTION_KEYS, place)
        string_field(option, "label", place)
        line = string_field(option, "line", place)
        if "input" in option:
            string_field(option, "input", place)
            if number == 1:
                fail(f"{place}: первый вариант — значение по умолчанию, он не может требовать ввода")
        if "choice" in option:
            string_list(option, "choice", place)
        if ("{input}" in line) != ("input" in option):
            fail(f"{place}: {{input}} и поле input должны быть вместе")
        if ("{choice}" in line) != ("choice" in option):
            fail(f"{place}: {{choice}} и поле choice должны быть вместе")


@functools.cache
def page_source():
    """Исходники страницы: имя файла в scripts/page/ → текст."""
    return {name: read(PAGE / name) for name in ("page.html", "page.css", "logic.js", "app.js")}


def load():
    """Читает src/, проверяет схему и возвращает (сценарии, текст общих правил)."""
    try:
        data = tomllib.loads(read(SRC / "scenarios.toml"))
    except tomllib.TOMLDecodeError as error:
        fail(f"scenarios.toml: {error}")
    check_keys(data, {"scenario"}, "scenarios.toml")
    items = table_list(data, "scenario", "scenarios.toml")
    if not items:
        fail("scenarios.toml: нет ни одного [[scenario]]")
    rules = read(SRC / "rules.md")
    scenarios, owners = [], {}
    for number, item in enumerate(items, 1):
        file = item.get("file")
        where = f"scenarios.toml, сценарий {file}" if isinstance(file, str) else f"scenarios.toml, сценарий №{number}"
        check_keys(item, SCENARIO_KEYS, where)
        for key in ("file", "name", "when", "result", "never"):
            string_field(item, key, where)
        match = FILE_NAME.fullmatch(file)
        if not match:
            fail(f"{where}: имя файла должно быть вида NN-имя.md: две цифры, дефис, имя из латинских строчных букв, "
                 f"цифр и дефисов")
        num, sid = match.groups()
        for kind, value in (("file", file), ("num", num), ("id", sid)):
            if (kind, value) in owners:
                fail(f"{where}: {kind} «{value}» уже занят сценарием {owners[kind, value]}")
            owners[kind, value] = file
        params = table_list(item, "param", where)
        keys = []
        for position, param in enumerate(params, 1):
            key = param.get("key")
            place = f"{where}, параметр «{key}»" if isinstance(key, str) else f"{where}, параметр №{position}"
            string_field(param, "key", place)
            if key in keys:
                fail(f"{place}: имя параметра повторяется")
            keys.append(key)
            validate_param(place, param)
        scenarios.append(dict(item, id=sid, num=num, params=params))
    reserved = set(re.findall(r"""(?<![\w-])id=["']([^"'@{]+)""", "".join(page_source().values())))  # id служебных элементов страницы
    for s in scenarios:
        # Страница строит id вида «<id>», «<id>-title», «<id>-0-label»…: id сценария не должен совпасть с чужим
        # id (служебным или другого сценария) и не должен быть его началом или продолжением через дефис.
        others = reserved | {o["id"] for o in scenarios if o is not s}
        clash = [o for o in sorted(others) if s["id"] == o or s["id"].startswith(o + "-") or o.startswith(s["id"] + "-")]
        if clash:
            fail(f"scenarios.toml, сценарий {s['file']}: id «{s['id']}» пересекается с id «{clash[0]}» на странице; "
                 f"переименуй файл")
    listed = {s["file"] for s in scenarios}
    present = {path.name for path in (SRC / "scenarios").glob("*.md")}
    if listed != present:
        fail(f"src/scenarios/ и scenarios.toml расходятся: {sorted(listed ^ present)}")
    for scenario in scenarios:
        source = read(SRC / "scenarios" / scenario["file"])
        for token in (PARAMS, RULES):
            if source.count(token) != 1:
                fail(f"src/scenarios/{scenario['file']}: {token} должен встречаться ровно один раз")
        scenario.update(source=source, text=source.replace(RULES, rules.strip()))
    return scenarios, rules


def default_state(param):
    if "choices" in param:
        return {"picked": list(param["choices"]) if param["default"] == "all" else []}
    return {"index": 0}


def clean(value):
    """Ввод пользователя: подряд идущие пробельные символы — один пробел, по краям пробелов нет."""
    return SPACES.sub(" ", value).strip(" ")


def param_text(param, state):
    """Строка параметра в промпте. Та же логика повторена в paramText на странице."""
    if "choices" in param:
        selected = [c for c in param["choices"] if c in state["picked"]]
        rest = [c for c in param["choices"] if c not in selected]
        template = param["none"] if not selected else param["all"] if not rest else param["some"]
        return (template.replace("{all}", ", ".join(param["choices"]))
                .replace("{selected}", ", ".join(selected)).replace("{rest}", ", ".join(rest)))
    option = param["option"][state["index"]]
    text = option["line"]
    if "input" in option:
        text = text.replace("{input}", clean(state.get("input", "")) or MISSING)
    if "choice" in option:
        text = text.replace("{choice}", state.get("choice") or option["choice"][0])
    return text


def params_block(scenario, states=None):
    if not scenario["params"]:
        return ""
    states = states or [default_state(param) for param in scenario["params"]]
    lines = [f"- {param['key']}: {param_text(param, state)}" for param, state in zip(scenario["params"], states)]
    return "## Параметры\n" + "\n".join(lines)


def split(scenario):
    """Текст до и после раздела параметров; страница собирает промпт так же."""
    if not scenario["params"]:
        return scenario["text"].replace(PARAMS + "\n\n", ""), ""
    return scenario["text"].split(PARAMS)


def assemble(scenario, states=None):
    before, after = split(scenario)
    return before + params_block(scenario, states) + after


# --- Случаи для проверок: сверка страницы с Python и предел длины ------------------------------

FULL_LIMIT = 100  # полный перебор сочетаний вариантов параметров, пока их не больше
# Пробельные символы во вводе: NBSP, табуляция, перевод строки, U+FEFF, U+0085, U+001C.
SPACE_PROBES = ("\u00a0", "\t", "\n", "\ufeff", "\x85", "\x1c")


def filled_input(index):
    """Ввод со своим номером параметра: общее состояние ввода у двух параметров дало бы чужое значение."""
    return f"  значение {index}   $& из поля "


def param_variants(param, index, thorough):
    """Состояния одного параметра: каждый вариант, у варианта с вводом — заполненный и пустой ввод (при thorough
    еще ввод с каждым пробельным символом по краям и внутри и ввод из одних пробельных символов), у варианта
    с выбором — каждое значение; для множественного выбора — пустой, полный и два частичных набора."""
    if "choices" in param:
        picked = ([], param["choices"], param["choices"][:1], param["choices"][1:])
        return [{"picked": list(p)} for p in picked]
    variants = []
    for position, option in enumerate(param["option"]):
        inputs = [""]
        if "input" in option:
            inputs = [filled_input(index), ""]
            if thorough:
                inputs += [f"{c}ввод{c}{c}значение{c}" for c in SPACE_PROBES] + ["".join(SPACE_PROBES)]
        for choice in option.get("choice", [""]):
            variants += [{"index": position, "input": value, "choice": choice} for value in inputs]
    return variants


def contrast_state(param, index):
    """Состояние, отличное от состояния по умолчанию: последний вариант, его ввод и последнее значение выбора."""
    if "choices" in param:
        return {"picked": param["choices"][1:] if param["default"] == "all" else param["choices"][:1]}
    position = len(param["option"]) - 1
    option = param["option"][position]
    return {"index": position, "input": filled_input(index) if "input" in option else "",
            "choice": option.get("choice", [""])[-1]}


def variant_states(scenario):
    """Наборы состояний параметров сценария:
    1) все по умолчанию; 2) каждый вариант каждого параметра при остальных по умолчанию (с вводом и пробельными
    символами); 3) полный перебор сочетаний, если их не больше FULL_LIMIT; 4) все параметры не по умолчанию сразу."""
    params = scenario["params"]
    defaults = [default_state(param) for param in params]
    states = [defaults]
    for i, param in enumerate(params):
        states += [defaults[:i] + [variant] + defaults[i + 1:] for variant in param_variants(param, i, True)]
    if params:
        combos = [param_variants(param, i, False) for i, param in enumerate(params)]
        if math.prod(len(c) for c in combos) <= FULL_LIMIT:
            states += [list(combo) for combo in itertools.product(*combos)]
        states.append([contrast_state(param, i) for i, param in enumerate(params)])
    unique = {json.dumps(s, ensure_ascii=False, sort_keys=True): s for s in states}
    return list(unique.values())


def all_variants(scenarios):
    """(сценарий, состояния, собранный текст) — один раз для проверки длины и меток и для сверки страницы."""
    return [(s, states, assemble(s, states)) for s in scenarios for states in variant_states(s)]


def parity_cases(variants):
    return [{"id": s["id"], "states": states, "expected": text} for s, states, text in variants]


# --- Эталон: ожидаемые строки заданы вручную на синтетической модели, а не выводятся из кода сборки -----------

REFERENCE_PARAMS = [
    {"key": "Режим", "option": [
        {"label": "а", "line": "обычный."},
        {"label": "б", "line": "со вводом: {input}.", "input": "п"},
        {"label": "в", "line": "в «{choice}»: {input}.", "input": "п", "choice": ["раз", "два"]}]},
    {"key": "Набор", "choices": ["x", "y", "z"], "default": "none",
     "all": "все: {selected}.", "none": "ничего: {all}.", "some": "часть: {selected}; остальное: {rest}."},
    {"key": "Пара", "choices": ["p", "q"], "default": "all", "all": "{all}!", "none": "нет", "some": "{selected}/{rest}"},
]
REFERENCE_SCENARIO = {"text": f"до\n\n{PARAMS}\n\nпосле", "params": REFERENCE_PARAMS}
REFERENCE_CASES = [  # (состояния параметров, ожидаемый текст)
    (None, "до\n\n## Параметры\n- Режим: обычный.\n- Набор: ничего: x, y, z.\n- Пара: p, q!\n\nпосле"),
    ([{"index": 1, "input": " \u00a0 привет\t мир\n"}, {"picked": ["y"]}, {"picked": []}],
     "до\n\n## Параметры\n- Режим: со вводом: привет мир.\n- Набор: часть: y; остальное: x, z.\n- Пара: нет\n\nпосле"),
    ([{"index": 1, "input": "\ufeff\x85"}, {"picked": ["z", "y", "x"]}, {"picked": ["q"]}],
     f"до\n\n## Параметры\n- Режим: со вводом: {MISSING}.\n- Набор: все: x, y, z.\n- Пара: q/p\n\nпосле"),
    ([{"index": 2, "input": "z$&", "choice": "два"}, {"picked": ["z", "x"]}, {"picked": ["p", "q"]}],
     "до\n\n## Параметры\n- Режим: в «два»: z$&.\n- Набор: часть: x, z; остальное: y.\n- Пара: p, q!\n\nпосле"),
    ([{"index": 2, "input": "a"}, {"picked": []}, {"picked": ["q", "p"]}],
     "до\n\n## Параметры\n- Режим: в «раз»: a.\n- Набор: ничего: x, y, z.\n- Пара: p, q!\n\nпосле"),
]
REFERENCE_NO_PARAMS = ({"text": f"до\n\n{PARAMS}\n\nпосле", "params": []}, "до\n\nпосле")


def check_reference(errors):
    for number, (states, expected) in enumerate(REFERENCE_CASES, 1):
        actual = assemble(REFERENCE_SCENARIO, states)
        if actual != expected:
            errors.append(f"эталон {number}: ожидалось {expected!r}, собрано {actual!r}")
    scenario, expected = REFERENCE_NO_PARAMS
    if assemble(scenario) != expected:
        errors.append(f"эталон без параметров: ожидалось {expected!r}, собрано {assemble(scenario)!r}")


# --- README: таблицы между маркерами -----------------------------------------------------------

def or_list(items):
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} или {items[-1]}"


def option_label(option):
    note = ""
    if "input" in option and "choice" in option:
        note = f" — со своим значением и выбором: {or_list(option['choice'])}"
    elif "input" in option:
        note = " — со своим значением"
    elif "choice" in option:
        note = f" — с выбором: {or_list(option['choice'])}"
    return option["label"].rstrip("…") + note


def readme_tables(scenarios, built):
    rows = ["| № | Промпт | Когда | Результат | Чего не делает | Символов |", "|---|---|---|---|---|---:|"]
    for s, text in zip(scenarios, built):
        size = f"{len(text):,}".replace(",", "\u00a0")
        rows.append(f"| {s['num']} | [{s['name']}](prompts/{s['file']}) | {s['when']} | {s['result']} | {s['never']} | {size} |")
    params = ["| Промпт | Параметр | Варианты |", "|---|---|---|"]
    for s in scenarios:
        for param in s["params"]:
            if "choices" in param:
                default = "все отмечены" if param["default"] == "all" else "ничего не отмечено"
                variants = f"любые из: {', '.join(param['choices'])} (по умолчанию {default})"
            else:
                labels = [option_label(o) for o in param["option"]]
                variants = f"{labels[0]} (по умолчанию); " + "; ".join(labels[1:])
            params.append(f"| {s['num']} {s['name']} | {param['key']} | {variants} |")
    return {"scenarios": "\n".join(rows), "params": "\n".join(params)}


def render_readme(text, tables):
    for name, table in tables.items():
        pattern = rf"(<!-- {name}:begin -->\n)(?:.*?\n)?(<!-- {name}:end -->)"
        if not re.search(pattern, text, re.S):
            fail(f"README: нет маркеров {name}:begin / {name}:end")
        text = re.sub(pattern, lambda m: m.group(1) + table + "\n" + m.group(2), text, count=1, flags=re.S)
    return text


# --- Проверки ----------------------------------------------------------------------------------

MARK = re.compile(r"@([A-Z_]+)@")  # метка страницы (fill) и то, что похоже на нее в src/
LABEL = re.compile(r"\{[a-zа-яё_]+\}|\{\{|\}\}", re.I)  # незаполненная метка в собранном тексте
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
INLINE_LINK = re.compile(r"\]\(\s*(?:<([^>\n]*)>|((?:[^()\s]|\([^()\s]*\))*))")
REF_DEFINITION = re.compile(r"^ {0,3}\[(?!\^)([^\]\n]+)\]:[ \t]*(?:<([^>\n]*)>|(\S+))", re.M)
REF_USE = re.compile(r"\[(?!\^)([^\]\n]+)\]\[([^\]\n]*)\]")
HTML_LINK = re.compile(r"""<(?:img\b[^>]*?\bsrc|a\b[^>]*?\bhref)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
HTML_ANCHOR = re.compile(r"""<\w+\b[^>]*?\b(?:id|name)\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.I)
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", re.M)
CROSS_REF = re.compile(r"(?<!\w)(правил(?:о|а|у|ом|е|ам|ами|ах)?|шаг(?:а|у|ом|е|и|ов|ам|ами|ах)?)\s+"
                       r"(\d+(?:\s*(?:,|и|или|–|—|-)\s*\d+)*)", re.I)


def repo_files():
    """Тексты проверяемых файлов: путь от корня → текст. Фиксированные корни, без git и без обхода всего дерева, чтобы
    неотслеживаемые файлы не давали красной проверки: *.md в корне и в docs/, src/, .github/ (собранный prompts/ не
    входит), *.toml — из src/."""
    paths = list(ROOT.glob("*.md"))
    for folder in ("docs", "src", ".github"):
        paths += (ROOT / folder).rglob("*.md")
    paths += SRC.rglob("*.toml")
    return {path.relative_to(ROOT).as_posix(): read(path) for path in sorted(set(paths)) if path.is_file()}


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


def strip_code(text):
    """Текст без кода: блоки и фрагменты заменены пробелами, номера строк и смещения сохранены."""
    lines, fence = [], None
    for line in text.split("\n"):
        if fence is None:
            opening = FENCE.match(line)
            if opening:
                fence = opening.group(1)
                line = " " * len(line)
            else:
                line = INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line)
        else:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = None
            line = " " * len(line)
        lines.append(line)
    return "\n".join(lines)


def slug(heading):
    """Якорь заголовка по правилам GitHub: без разметки, строчные буквы, из знаков только дефис и подчеркивание."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text.replace("`", "")).strip().lower()
    text = re.sub(r"(?<!\w)_+(?=\w)|(?<=\w)_+(?!\w)", "", text)  # _курсив_; подчеркивания внутри слов остаются
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(text):
    """Якоря Markdown-текста: заголовки (повтор получает суффикс -1, -2…) и HTML-атрибуты id и name.
    Заголовки с подчеркиванием строкой (setext) не распознаются: ссылка на такой заголовок будет ложно битой.
    Ограничение slug: подчеркивание у границы слова считается курсивом (заголовок «_private» получит якорь «private»)."""
    code_free = strip_code(text)
    found, seen = set(), {}
    for match in HEADING.finditer(code_free):
        base = slug(text[match.start(1):match.end(1)])  # найден в тексте без кода, а слова берутся из исходника
        found.add(base if base not in seen else f"{base}-{seen[base]}")
        seen[base] = seen.get(base, 0) + 1
    found.update((a or b).lower() for a, b in (m.groups() for m in HTML_ANCHOR.finditer(code_free)))
    return found


def check_links(files, errors):
    """Ссылки во всех Markdown-файлах: inline, по определению, <img src>, <a href>; файлы и якоря должны существовать.
    Внешние (со схемой или //хост) не открываются."""
    cache = {}

    def anchors_of(path):
        if path not in cache:
            rel = path.relative_to(ROOT).as_posix()
            cache[path] = anchors(files[rel] if rel in files else read(path))
        return cache[path]

    def problem(doc, target):
        if target.startswith("//") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            return None
        route, _, fragment = target.partition("#")
        route = unquote(route.partition("?")[0])
        if route:
            base = ROOT if route.startswith("/") else (ROOT / doc).parent
            path = Path(os.path.normpath(base / route.lstrip("/")))
            if ROOT != path and ROOT not in path.parents:
                return "ведет за пределы репозитория"
            if not path.exists():
                return "нет такого файла"
        else:
            path = ROOT / doc
        if fragment and path.suffix == ".md" and path.is_file() and unquote(fragment).lower() not in anchors_of(path):
            return f"нет якоря #{fragment}" + ("" if route else " в этом файле")
        return None

    for doc, text in files.items():
        if not doc.endswith(".md"):
            continue
        body = strip_code(text)
        labels = {" ".join(m.group(1).split()).casefold() for m in REF_DEFINITION.finditer(body)}
        targets = [(m.start(), m.group(1) if m.group(1) is not None else m.group(2)) for m in INLINE_LINK.finditer(body)]
        targets += [(m.start(), m.group(2) if m.group(2) is not None else m.group(3)) for m in REF_DEFINITION.finditer(body)]
        targets += [(m.start(), next(g for g in m.groups() if g is not None)) for m in HTML_LINK.finditer(body)]
        for offset, target in sorted(targets):
            reason = problem(doc, target)
            if reason:
                errors.append(f"{doc}:{line_of(body, offset)}: битая ссылка {target}: {reason}")
        for match in REF_USE.finditer(body):
            label = " ".join((match.group(2) or match.group(1)).split()).casefold()
            if label not in labels:
                errors.append(f"{doc}:{line_of(body, match.start())}: ссылка [{match.group(2) or match.group(1)}] без определения")


def numbered(text):
    """Номера пунктов нумерованных списков."""
    return {int(n) for n in re.findall(r"^[ \t]{0,3}(\d+)\.\s", text, re.M)}


def strings(value, path=""):
    """Все строки значения TOML с путем к ним, например param[2].option[1].line."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for number, item in enumerate(value, 1):
            yield from strings(item, f"{path}[{number}]")


def check_numbers(scenarios, rules, errors):
    """«Правило N» и «шаг N» ссылаются на существующий пункт: правила — нумерованный список src/rules.md; шаги — списки
    сценария, к которому относится текст. Общие правила попадают в каждый сценарий, поэтому шаг из них должен быть везде."""
    rule_numbers = numbered(rules)
    steps = {s["file"]: numbered(s["source"]) for s in scenarios}

    def verify(place, text, files):
        for match in CROSS_REF.finditer(text):
            is_rule = match.group(1).lower().startswith("правил")
            numbers, previous, dash = [], None, False
            for token in re.findall(r"\d+|[–—-]", match.group(2)):  # «4 и 5», «4, 5», «4–6»
                if token.isdigit():
                    value = int(token)
                    numbers += range(previous + 1, value + 1) if dash else [value]
                    previous, dash = value, False
                else:
                    dash = True
            for number in numbers:
                lacking = [] if not is_rule or number in rule_numbers else ["src/rules.md"]
                lacking += [] if is_rule else [file for file in files if number not in steps[file]]
                if lacking:
                    errors.append(f"{place(match)}: «{match.group(0)}» — нет {'правила' if is_rule else 'шага'} {number} "
                                  f"в {', '.join(lacking)}")

    verify(lambda m: f"src/rules.md:{line_of(rules, m.start())}", rules, list(steps))
    for s in scenarios:
        verify(lambda m, s=s: f"src/scenarios/{s['file']}:{line_of(s['source'], m.start())}", s["source"], [s["file"]])
        for path, text in strings({key: s[key] for key in SCENARIO_KEYS if key in s}):
            verify(lambda m, path=path, s=s: f"scenarios.toml, {s['file']}, {path}", text, [s["file"]])


def describe(states):
    return "по умолчанию" if states is None else json.dumps(states, ensure_ascii=False)[:200]


def check(scenarios, rules, built, variants, files, readme, current_readme):
    errors = []
    # Собранное совпадает с файлами в репозитории.
    expected = {f"prompts/{s['file']}": text for s, text in zip(scenarios, built)}
    actual = {f"prompts/{p.name}": read(p) for p in PROMPTS.glob("*.md")}
    stale = [path for path in sorted(expected.keys() | actual.keys()) if expected.get(path) != actual.get(path)]
    if current_readme != readme:
        stale.append("README.md")
    if stale:
        errors.append(f"{', '.join(stale)}: не совпадает со сборкой из src/ — запусти python3 scripts/build.py")
    # Названия клиентов, моделей и версий — только в src/: промпты и страница собраны из него.
    for name, text in files.items():
        if name.startswith("src/"):
            for pattern in FORBIDDEN:
                for match in pattern.finditer(text):
                    errors.append(f"{name}:{line_of(text, match.start())}: недопустимое упоминание «{match.group(0)}»")
            for match in MARK.finditer(text):
                errors.append(f"{name}:{line_of(text, match.start())}: «{match.group(0)}» выглядит как метка шаблона страницы; "
                              f"перефразируй")
    for sample, forbidden in FORBIDDEN_SAMPLES:
        if any(p.search(sample) for p in FORBIDDEN) != forbidden:
            errors.append(f"FORBIDDEN: «{sample}» должно {'запрещаться' if forbidden else 'проходить'}")
    # Предел длины и метки: по умолчанию и на всех вариантах, одна строка на сценарий и причину.
    for s, text in zip(scenarios, built):
        texts = [(None, text)] + [(states, t) for v, states, t in variants if v is s]
        long = [(len(t), states) for states, t in texts if len(t) > PROMPT_LIMIT]
        if long:
            size, states = max(long, key=lambda item: item[0])
            errors.append(f"{s['file']}: {len(long)} из {len(texts)} вариантов длиннее {PROMPT_LIMIT}; "
                          f"худший — {size} символов при состояниях: {describe(states)}")
        unfilled = [(sorted({m.group(0) for m in LABEL.finditer(t)}), states) for states, t in texts if LABEL.search(t)]
        if unfilled:
            labels, states = unfilled[0]
            errors.append(f"{s['file']}: незаполненные метки {', '.join(labels)} в {len(unfilled)} из {len(texts)} вариантов, "
                          f"например при состояниях: {describe(states)}")
    check_reference(errors)
    check_links(files, errors)
    check_numbers(scenarios, rules, errors)
    return errors


# --- Страница ----------------------------------------------------------------------------------

ICON = {
    "check": '<svg class="icon icon-check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.6l2.8 2.8 6.2-6.6"/></svg>',
    "chevron": '<svg class="icon icon-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.5l4 3.5 4-3.5"/></svg>',
    "copy": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.6"/>'
            '<path d="M10.5 5.5V3.6A1.1 1.1 0 0 0 9.4 2.5H3.6A1.1 1.1 0 0 0 2.5 3.6v5.8a1.1 1.1 0 0 0 1.1 1.1h1.9"/></svg>',
    "download": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.6 7.3L8 10.7l3.4-3.4M3 13.5h10"/></svg>',
    "reset": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8a4.8 4.8 0 1 0 1.4-3.4M3 2.6v2.8h2.8"/></svg>',
    "auto": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5"/><path d="M8 2.5a5.5 5.5 0 0 1 0 11z" fill="currentColor" stroke="none"/></svg>',
    "sun": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.8"/>'
           '<path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2"/></svg>',
    "moon": '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.2 9.6A5.6 5.6 0 0 1 6.4 2.8a5.6 5.6 0 1 0 6.8 6.8z"/></svg>',
}


def inline(text):
    text = html.escape(text, quote=False)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def param_field(scenario, index, param):
    fid = html.escape(f"{scenario['id']}-{index}")
    esc = html.escape
    help_html = f'<p class="help" id="{fid}-help">{inline(param["help"])}</p>'
    if "choices" in param:
        checked = " checked" if param["default"] == "all" else ""
        chips = "".join(f'<label class="chip"><input type="checkbox" value="{esc(c)}"{checked}>'
                        f'<span class="chip-box" aria-hidden="true">{ICON["check"]}</span><span>{esc(c)}</span></label>'
                        for c in param["choices"])
        return (f'<div class="field wide" data-index="{index}" role="group" aria-labelledby="{fid}-label" '
                f'aria-describedby="{fid}-help"><span class="label" id="{fid}-label">{esc(param["key"])}</span>'
                f'<div class="chips">{chips}</div>{help_html}</div>')
    options = "".join(
        f'<li class="option" role="option" id="{fid}-o{i}" data-value="{i}" aria-selected="{"true" if i == 0 else "false"}">'
        f'<span>{esc(o["label"])}</span>{ICON["check"]}</li>' for i, o in enumerate(param["option"]))
    extra = ""
    if any("input" in o for o in param["option"]):
        extra += (f'<input type="text" class="extra-input" hidden autocomplete="off" spellcheck="false" '
                  f'aria-label="{esc(param["key"])}: значение" aria-describedby="{fid}-note">'
                  f'<p class="note" id="{fid}-note" hidden>Нужно значение: без него промпт не скопируется.</p>')
    if any("choice" in o for o in param["option"]):
        extra += f'<div class="segmented extra-choice" role="radiogroup" hidden aria-label="{esc(param["key"])}: область"></div>'
    extra = f'<div class="extra" data-open="false"><div>{extra}</div></div>' if extra else ""
    # Комбобокс по образцу WAI-ARIA APG «Select-Only Combobox»: кнопка-триггер + список в popover.
    return (f'<div class="field" data-index="{index}"><label class="label" id="{fid}-label" for="{fid}">{esc(param["key"])}</label>'
            f'<div class="select"><button type="button" class="select-trigger" id="{fid}" role="combobox" '
            f'aria-haspopup="listbox" aria-expanded="false" aria-controls="{fid}-list" aria-describedby="{fid}-help" '
            f'popovertarget="{fid}-pop" data-value="0"><span class="select-value">{esc(param["option"][0]["label"])}</span>'
            f'{ICON["chevron"]}</button><div class="listbox-pop" id="{fid}-pop" popover>'
            f'<p class="listbox-title" aria-hidden="true">{esc(param["key"])}</p>'
            f'<ul class="listbox" id="{fid}-list" role="listbox" aria-labelledby="{fid}-label">{options}</ul></div></div>'
            f'{extra}{help_html}</div>')


def scenario_section(scenario, built):
    fields = "".join(param_field(scenario, i, p) for i, p in enumerate(scenario["params"]))
    sid, num, file = (html.escape(scenario[key]) for key in ("id", "num", "file"))
    params = (f'<div class="params" role="group" aria-labelledby="{sid}-params"><div class="params-head">'
              f'<h3 id="{sid}-params">Параметры</h3><button type="button" class="btn-text" data-action="reset" disabled>'
              f'{ICON["reset"]}Сбросить</button></div><div class="fields">{fields}</div></div>') if fields else ""
    return f"""
<section class="scenario" id="{sid}" aria-labelledby="{sid}-title" data-file="{file}">
  <h2 id="{sid}-title"><span class="num">{num}</span>{html.escape(scenario['name'])}</h2>
  <dl class="facts">
    <div><dt>Когда</dt><dd>{inline(scenario['when'])}</dd></div>
    <div><dt>Результат</dt><dd>{inline(scenario['result'])}</dd></div>
    <div><dt>Чего не делает</dt><dd>{inline(scenario['never'])}</dd></div>
  </dl>
  {params}
  <div class="bar-sentinel" aria-hidden="true"></div>
  <div class="bar">
    <button type="button" class="btn btn-primary" data-action="copy"><span class="btn-icons">{ICON["copy"]}{ICON["check"]}</span><span class="btn-label">Скопировать промпт</span></button>
    <button type="button" class="btn btn-secondary" data-action="download"><span class="btn-icons">{ICON["download"]}{ICON["check"]}</span><span class="btn-label">Скачать .md</span></button>
    <span class="meta"></span>
  </div>
  <pre class="prompt" tabindex="0" aria-label="Текст промпта">{html.escape(built, quote=False)}</pre>
</section>"""


# Метки страницы вида @ИМЯ@ подставляются за один проход: вставленный текст (сценарии, данные, правила) повторно не
# разбирается, поэтому метка в тексте сценария остается буквальным текстом. Тот же вид меток запрещен в src/.
# Данные лежат в <script type="application/json">: эти символы заменены escape-последовательностями JSON, чтобы текст
# не мог ни закрыть скрипт или открыть комментарий (<!--, </script>), ни сломаться на разделителях строк JS.
JSON_ESCAPES = {ord(c): f"\\u{ord(c):04x}" for c in "<>&\u2028\u2029"}


def fill(text, values, where):
    """Подставляет метки @ИМЯ@ из словаря за один проход; неизвестная метка и неиспользованное значение — ошибка."""
    unused = sorted(set(values) - {m[1] for m in MARK.finditer(text)})
    if unused:
        fail(f"{where}: нет меток {', '.join(f'@{name}@' for name in unused)}")

    def put(match):
        if match[1] not in values:
            fail(f"{where}: неизвестная метка {match[0]}")
        return values[match[1]]
    return MARK.sub(put, text)


def render_site(scenarios, built):
    """Текст страницы из scripts/page/ и сценариев; ошибка шаблона (неизвестная метка и т. п.) — fail."""
    data = [{"id": s["id"], "file": s["file"], "before": split(s)[0], "after": split(s)[1], "params": s["params"]}
            for s in scenarios]
    rail = "".join(f'<li><a href="#{html.escape(s["id"])}" data-id="{html.escape(s["id"])}">'
                   f'<span class="num">{html.escape(s["num"])}</span><span class="name">{html.escape(s["name"])}</span></a></li>'
                   for s in scenarios)
    sha = html.escape(os.environ.get("GITHUB_SHA", "")[:7])
    built_from = f' Собрано из коммита <a href="https://github.com/{html.escape(REPO)}/commit/{sha}">{sha}</a>.' if sha else ""
    source = page_source()
    page = fill(source["page.html"], {
        "STYLE": fill(source["page.css"], {}, "page.css"),
        "LOGIC": fill(source["logic.js"], {"WHITESPACE": WHITESPACE}, "logic.js"),
        "APP": fill(source["app.js"], {}, "app.js"),
        "RAIL": rail,
        "SECTIONS": "".join(scenario_section(s, t) for s, t in zip(scenarios, built)),
        "DATA": json.dumps({"missing": MISSING, "scenarios": data}, ensure_ascii=False).translate(JSON_ESCAPES),
        "ICON_AUTO": ICON["auto"], "ICON_SUN": ICON["sun"], "ICON_MOON": ICON["moon"],
        "REPO": html.escape(REPO), "BUILT": built_from,
    }, "page.html")
    return page


def build_site(page, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{out / 'index.html'}: {len(page.encode())} байт")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="только проверить, ничего не записывая")
    parser.add_argument("--site", type=Path, help="каталог для страницы GitHub Pages")
    parser.add_argument("--parity-cases", type=Path, help="записать случаи для scripts/parity.mjs")
    args = parser.parse_args()
    scenarios, rules = load()
    built = [assemble(s) for s in scenarios]
    current_readme = read(README)
    readme = render_readme(current_readme, readme_tables(scenarios, built))
    variants = all_variants(scenarios) if args.check or args.parity_cases else []
    page = render_site(scenarios, built) if args.check or args.site else None  # --check собирает страницу и без записи
    if args.check:
        errors = check(scenarios, rules, built, variants, repo_files(), readme, current_readme)
        for s, text in zip(scenarios, built):
            print(f"prompts/{s['file']}: {len(text)} символов")
        if errors:
            sys.stdout.flush()
            print("\n".join(["", "ОШИБКИ:", *errors]), file=sys.stderr)
            sys.exit(1)
        print("OK")
    else:
        PROMPTS.mkdir(exist_ok=True)
        for stale in set(PROMPTS.glob("*.md")) - {PROMPTS / s["file"] for s in scenarios}:
            stale.unlink()
        for s, text in zip(scenarios, built):
            (PROMPTS / s["file"]).write_text(text, encoding="utf-8")
        README.write_text(readme, encoding="utf-8")
        print(f"собрано: {len(built)} промптов, таблицы README")
    if args.site:
        build_site(page, args.site)
    if args.parity_cases:
        args.parity_cases.write_text(json.dumps(parity_cases(variants), ensure_ascii=False), encoding="utf-8")
        print(f"{args.parity_cases}: случаев {len(variants)}")


if __name__ == "__main__":
    main()
