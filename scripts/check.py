#!/usr/bin/env python3
"""Проверка согласованности промптов. Запуск: python3 scripts/check.py"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
SETUP = "00-setup.md"
BEGIN, END = "<!-- BEGIN agent-contract -->", "<!-- END agent-contract -->"
CONTRACT_HEADING = "## Общий контракт агента"
CONTRACT_END_LINE = "Конец общего контракта агента"
# Блок загружается в каждую сессию; часть клиентов обрезает все инструкции на 32 КиБ.
CONTRACT_LIMIT_BYTES = 18 * 1024

# Промпты описывают возможности по сути: без названий клиентов, расширений и версий.
FORBIDDEN = [
    r"\b(codex|claude|cursor|gemini|copilot|windsurf|aider|cline|roo|jules|zed|openai|anthropic|gpt)\b",
    r"\borchestration:", r"\$[a-z][\w-]+", r"create_goal", r"(?<![\w/-])/goal\b",
    r"policy-version", r"unified-agent-policy", r"\bv\d+\b",
    r"\b\d+\.\d+\.\d{1,3}\b",  # номер версии; даты с годом из 4 цифр не совпадают
    r"редакци\w*\s+\d", r"плагин",
]

errors = []


def section(text, title):
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else None


texts = {path.name: path.read_text(encoding="utf-8") for path in sorted(PROMPTS.glob("*.md"))}
if SETUP not in texts:
    errors.append(f"prompts/{SETUP}: файл отсутствует")

for name, text in texts.items():
    if not text.startswith("# "):
        errors.append(f"{name}: первая строка должна быть заголовком «# …»")
    for pattern in FORBIDDEN:
        for match in re.finditer(pattern, text, re.I):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{name}:{line}: недопустимое упоминание «{match.group(0)}»")

# Раздел «Основа» во всех сценариях, кроме подключения, совпадает дословно
# и опознает обрезанный контракт по его последней строке.
bases = {name: section(text, "Основа") for name, text in texts.items() if name != SETUP}
for name, base in bases.items():
    if base is None:
        errors.append(f"{name}: нет раздела «## Основа»")
    elif CONTRACT_END_LINE not in base:
        errors.append(f"{name}: «Основа» не ссылается на строку «{CONTRACT_END_LINE}»")
if len({base for base in bases.values() if base is not None}) > 1:
    errors.append("раздел «Основа» различается между сценариями: " + ", ".join(sorted(bases)))

# Контракт — ровно один блок, только в сценарии подключения.
setup = texts.get(SETUP, "")
if setup.count(BEGIN) != 1 or setup.count(END) != 1:
    errors.append(f"{SETUP}: должна быть ровно одна пара маркеров контракта")
else:
    contract = setup[setup.index(BEGIN):setup.index(END) + len(END)]
    lines = [line for line in contract.splitlines() if line.strip()]
    if lines[1] != CONTRACT_HEADING:
        errors.append(f"контракт: первая строка после маркера — не «{CONTRACT_HEADING}»")
    if lines[-2].rstrip(".") != CONTRACT_END_LINE:
        errors.append(f"контракт: последняя строка перед маркером — не «{CONTRACT_END_LINE}»")
    size = len(contract.encode())
    if size > CONTRACT_LIMIT_BYTES:
        errors.append(f"контракт: {size} байт, предел {CONTRACT_LIMIT_BYTES}")
    print(f"контракт: {len(contract)} символов, {size} байт")
for name, text in texts.items():
    if name != SETUP and (BEGIN in text or END in text):
        errors.append(f"{name}: маркеры контракта допустимы только в {SETUP}")

# Относительные ссылки README и AGENTS.md ведут на существующие файлы.
for doc in ("README.md", "AGENTS.md"):
    path = ROOT / doc
    if not path.exists():
        errors.append(f"{doc}: файл отсутствует")
        continue
    for target in re.findall(r"\]\(<?([^)#\s>]+)", path.read_text(encoding="utf-8")):
        if not re.match(r"[a-z]+:", target) and not (ROOT / target).exists():
            errors.append(f"{doc}: битая ссылка {target}")

for name, text in texts.items():
    print(f"{name}: {len(text)} символов")

if errors:
    sys.stdout.flush()
    print("\n".join(["", "ОШИБКИ:", *errors]), file=sys.stderr)
    sys.exit(1)
print("OK")
