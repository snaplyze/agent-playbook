// Smoke-тест собранной страницы в headless Chrome: страница работает так, как её видит пользователь. Без зависимостей:
// Node 22+ (WebSocket встроен) и Chrome DevTools Protocol. Запуск, как в CI (<tmp> — каталог вне репозитория):
//   python3 scripts/build.py --check --site <tmp>/site --parity-cases <tmp>/cases.json
//   node scripts/smoke.mjs <tmp>/site/index.html <tmp>/cases.json
// Аргументы: страница (файл или каталог с index.html) и случаи для сверки. Chrome: CHROME_BIN, иначе первый найденный
// из google-chrome, google-chrome-stable, chromium, chromium-browser.
//
// Что проверяется (всё через настоящие события мыши и клавиатуры, как у пользователя): начальный текст каждого
// сценария (<pre>, исходный HTML и то, что копируется) равен ожидаемому тексту случая «все по умолчанию»; выбор
// варианта в комбобоксе, ввод с лишними пробелами, выбор области, снятие и установка чипов дают текст случая из
// cases.json; пустое обязательное поле блокирует копирование и скачивание и подсвечивается; копирование кладет в
// буфер ровно показанный текст, скачивание дает файл NN-имя.md с тем же текстом; сброс возвращает умолчания,
// очищает ввод и восстанавливает чипы; на странице нет исключений и ошибок консоли.
//
// Страница отдается локальным сервером на 127.0.0.1. Внешние запросы (шрифты) подменяются пустым ответом: тест не
// зависит от сети. Анимации выключены эмуляцией prefers-reduced-motion: клики попадают в уже разложенные элементы.
import { spawn } from "node:child_process";
import { accessSync, constants, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { delimiter, join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const TOTAL_TIMEOUT = 120_000; // весь тест; обычно он занимает секунды
const CALL_TIMEOUT = 15_000; // один вызов CDP
const WAIT_TIMEOUT = 4_000; // ожидание изменения на странице
const QUIET = 250; // сколько ждать, чтобы убедиться, что копирования и скачивания не произошло
const SHOWN = 12; // сколько расхождений печатать подробно
const CHROME_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"];
const SENTINEL = "smoke: содержимое буфера до проверки";

const started = Date.now();
const cleanups = [];
const failures = [];
let checks = 0;

// Фатальная ошибка прекращает тест; наверху её ловит обработчик, который закрывает Chrome и сервер.
class Fatal extends Error {}
function die(message) {
  throw new Fatal(message);
}

let finishing = false;
function abort(message) {
  console.error(`smoke: ${message}`);
  finish(1);
}

function finish(code) {
  if (finishing) return;
  finishing = true;
  const done = Promise.allSettled(cleanups.splice(0).reverse().map((fn) => fn()));
  const timer = setTimeout(() => process.exit(code), 8_000); // зависшая очистка не держит процесс
  done.finally(() => {
    clearTimeout(timer);
    process.exit(code);
  });
}

const deadline = setTimeout(() => abort(`не уложились в ${TOTAL_TIMEOUT / 1000} с`), TOTAL_TIMEOUT);
deadline.unref();
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => abort(`прервано сигналом ${signal}`));
for (const event of ["uncaughtException", "unhandledRejection"]) {
  process.on(event, (error) => abort(error instanceof Fatal ? error.message : `внутренняя ошибка теста: ${error?.stack ?? error}`));
}

// --- Аргументы и файлы ---------------------------------------------------------------------------

const [pageArg, casesPath] = process.argv.slice(2);
if (!pageArg || !casesPath) die("запуск: node scripts/smoke.mjs <страница index.html или каталог> <cases.json>");
if (typeof WebSocket === "undefined") die(`нужен Node 22 или новее (встроенный WebSocket), запущен ${process.version}`);

function pagePathOf(arg) {
  try {
    return statSync(arg).isDirectory() ? join(arg, "index.html") : arg;
  } catch (error) {
    return die(`${arg}: не прочитать (${error.code ?? error.message})`);
  }
}
function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch (error) {
    return die(`${path}: не прочитать (${error.code ?? error.message})`);
  }
}
const pagePath = pagePathOf(pageArg);
readText(pagePath); // понятная ошибка до запуска браузера
const casesText = readText(casesPath);
let cases;
try {
  cases = JSON.parse(casesText);
} catch (error) {
  die(`${casesPath}: не разбирается как JSON: ${error.message}`);
}
if (!Array.isArray(cases) || !cases.length) die(`${casesPath}: нет ни одного случая`);

// --- Проверки --------------------------------------------------------------------------------------

const around = (text, at) => JSON.stringify(text.slice(Math.max(0, at - 30), at + 50));
function firstDifference(a, b) {
  const shared = Math.min(a.length, b.length);
  let i = 0;
  while (i < shared && a[i] === b[i]) i += 1;
  return i;
}
function fault(where, message) {
  failures.push(`${where}: ${message}`);
}
function ok(condition, where, message) {
  checks += 1;
  if (!condition) fault(where, message);
  return condition;
}
function sameText(actual, expected, where, what) {
  checks += 1;
  if (actual === expected) return true;
  const at = firstDifference(actual, expected);
  fault(where, `${what}: расхождение с позиции ${at} (длина ${actual.length}, ожидалось ${expected.length})\n` +
    `    получено: ${around(actual, at)}\n    ожидалось: ${around(expected, at)}`);
  return false;
}
const show = (value) => JSON.stringify(value);

// --- Chrome ------------------------------------------------------------------------------------------

function findChrome() {
  const explicit = process.env.CHROME_BIN;
  const executable = (path) => {
    try {
      accessSync(path, constants.X_OK);
      return statSync(path).isFile();
    } catch {
      return false;
    }
  };
  if (explicit) {
    if (!executable(explicit)) die(`CHROME_BIN=${explicit}: не исполняемый файл`);
    return explicit;
  }
  for (const dir of (process.env.PATH ?? "").split(delimiter).filter(Boolean)) {
    for (const name of CHROME_NAMES) if (executable(join(dir, name))) return join(dir, name);
  }
  return die(`Chrome не найден: задайте CHROME_BIN или установите один из ${CHROME_NAMES.join(", ")}`);
}

const FLAGS = [
  "--headless=new",
  "--remote-debugging-port=0", // порт выберет Chrome и запишет в DevToolsActivePort
  "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-sync", "--disable-background-networking", "--disable-component-update",
  "--disable-dev-shm-usage", // в контейнерах /dev/shm мал
  "--mute-audio", "--password-store=basic",
];

// Запускает Chrome и ждет DevToolsActivePort. Возвращает { child, wsUrl } или { error } с хвостом stderr.
async function launch(bin, extraFlags, profile) {
  const child = spawn(bin, [...FLAGS, ...extraFlags, `--user-data-dir=${profile}`, "about:blank"],
    { stdio: ["ignore", "ignore", "pipe"], detached: true });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr = (stderr + chunk).slice(-3000); });
  let exited = null;
  child.on("exit", (code, signal) => { exited = { code, signal }; });
  child.on("error", (error) => { exited = { code: null, signal: null, error }; });
  const portFile = join(profile, "DevToolsActivePort");
  const until = Date.now() + 20_000;
  while (Date.now() < until) {
    if (exited) break;
    if (existsSync(portFile)) {
      const [port, path] = readText(portFile).split("\n");
      if (port && path) return { child, wsUrl: `ws://127.0.0.1:${port}${path}` };
    }
    await sleep(40);
  }
  killTree(child);
  const why = exited ? `завершился сразу (код ${exited.code ?? exited.signal ?? exited.error?.message})` : "не открыл порт за 20 с";
  return { error: `Chrome ${why}${stderr.trim() ? `:\n${stderr.trim()}` : ""}` };
}

function killTree(child) {
  for (const signal of ["SIGTERM", "SIGKILL"]) {
    try {
      process.kill(-child.pid, signal); // вся группа: Chrome порождает вспомогательные процессы
    } catch {
      /* уже завершен */
    }
  }
}

// --- CDP ---------------------------------------------------------------------------------------------

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.next = 1;
    this.pending = new Map();
    this.handlers = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const call = this.pending.get(message.id);
        if (!call) return;
        this.pending.delete(message.id);
        clearTimeout(call.timer);
        if (message.error) call.reject(new Error(`${call.method}: ${message.error.message}`));
        else call.resolve(message.result);
      } else {
        for (const handler of this.handlers) handler(message);
      }
    });
    socket.addEventListener("close", () => {
      for (const call of this.pending.values()) call.reject(new Error(`${call.method}: соединение с Chrome закрыто`));
      this.pending.clear();
    });
  }

  static connect(url) {
    return new Promise((resolve, reject) => {
      const socket = new WebSocket(url);
      const timer = setTimeout(() => reject(new Error(`${url}: не подключиться за ${CALL_TIMEOUT / 1000} с`)), CALL_TIMEOUT);
      socket.addEventListener("open", () => { clearTimeout(timer); resolve(new Cdp(socket)); });
      socket.addEventListener("error", () => { clearTimeout(timer); reject(new Error(`${url}: ошибка соединения`)); });
    });
  }

  send(method, params = {}, sessionId) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: нет ответа за ${CALL_TIMEOUT / 1000} с`));
      }, CALL_TIMEOUT);
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }

  on(handler) {
    this.handlers.push(handler);
  }
}

async function waitFor(predicate, what, timeout = WAIT_TIMEOUT) {
  const until = Date.now() + timeout;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > until) throw new Error(`не дождались: ${what}`);
    await sleep(15);
  }
}

// --- Запуск ----------------------------------------------------------------------------------------

const temp = mkdtempSync(join(tmpdir(), "playbook-smoke-"));
cleanups.push(() => rmSync(temp, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }));

const server = createServer((request, response) => {
  const path = (request.url ?? "").split("?")[0];
  if (request.method === "GET" && (path === "/" || path === "/index.html")) {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    response.end(readFileSync(pagePath));
  } else {
    response.writeHead(404, { "content-type": "text/plain" });
    response.end("not found");
  }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
cleanups.push(() => new Promise((done) => { server.close(done); server.closeAllConnections(); }));
const origin = `http://127.0.0.1:${server.address().port}`;

const bin = findChrome();
let launched = await launch(bin, [], join(temp, "profile"));
if (launched.error) {
  // Без песочницы: на машинах, где пользовательские пространства имен запрещены (контейнеры, запуск от root),
  // Chrome с песочницей не стартует. Страница своя и статичная, поэтому повтор допустим.
  const retry = await launch(bin, ["--no-sandbox"], join(temp, "profile-no-sandbox"));
  if (retry.error) die(`${launched.error}\nповтор с --no-sandbox:\n${retry.error}`);
  console.error("smoke: Chrome с песочницей не стартовал, запущен с --no-sandbox");
  launched = retry;
}
cleanups.push(async () => {
  killTree(launched.child);
  await sleep(150);
});

const cdp = await Cdp.connect(launched.wsUrl);
const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
const { sessionId: sid } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
const page = (method, params) => cdp.send(method, params, sid);

// Ошибки страницы: необработанные исключения, console.error/assert и записи журнала уровня error.
const pageErrors = [];
cdp.on((message) => {
  if (message.sessionId !== sid) return;
  const { method, params } = message;
  if (method === "Runtime.exceptionThrown") {
    const d = params.exceptionDetails;
    pageErrors.push(`исключение: ${d.exception?.description ?? d.text} (${d.url ?? ""}:${d.lineNumber ?? "?"})`);
  } else if (method === "Runtime.consoleAPICalled" && ["error", "assert"].includes(params.type)) {
    pageErrors.push(`console.${params.type}: ${params.args.map((a) => a.value ?? a.description).join(" ")}`);
  } else if (method === "Log.entryAdded" && params.entry.level === "error") {
    pageErrors.push(`журнал: ${params.entry.text} ${params.entry.url ?? ""}`);
  } else if (method === "Fetch.requestPaused") {
    const local = params.request.url.startsWith(origin);
    // Внешнее (шрифты) отдаем пустым; свое пропускаем.
    (local
      ? page("Fetch.continueRequest", { requestId: params.requestId })
      : page("Fetch.fulfillRequest", { requestId: params.requestId, responseCode: 200,
        responseHeaders: [{ name: "content-type", value: "text/css" }], body: "" }))
      .catch((error) => pageErrors.push(`Fetch: ${error.message}`));
  }
});

// Скачивания приходят событиями браузера, а не страницы.
const downloads = [];
cdp.on((message) => {
  if (message.method === "Browser.downloadWillBegin") downloads.push({ ...message.params, state: "begin" });
  if (message.method === "Browser.downloadProgress") {
    const item = downloads.find((d) => d.guid === message.params.guid);
    if (item && message.params.state !== "inProgress") item.state = message.params.state;
  }
});

await page("Page.enable");
await page("Runtime.enable");
await page("Log.enable");
await page("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
await page("Emulation.setDeviceMetricsOverride", { width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false });
await page("Emulation.setFocusEmulationEnabled", { enabled: true });
await page("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
await cdp.send("Browser.grantPermissions", { origin, permissions: ["clipboardReadWrite", "clipboardSanitizedWrite"] });

async function ev(expression, { awaitPromise = false, userGesture = false } = {}) {
  const result = await page("Runtime.evaluate", { expression, returnByValue: true, awaitPromise, userGesture });
  if (result.exceptionDetails) {
    throw new Error(`в странице: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}\n      выражение: ${expression.replace(/\s+/g, " ").slice(0, 160)}`);
  }
  return result.result.value;
}

// --- Действия пользователя ------------------------------------------------------------------------------

async function click(selector, { scroll = true } = {}) {
  const point = await ev(`(() => {
    const el = document.querySelector(${show(selector)});
    if (!el) return { error: "нет элемента" };
    if (${scroll}) el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return { error: "элемент не виден" };
    const x = r.left + r.width / 2, y = r.top + r.height / 2, hit = document.elementFromPoint(x, y);
    if (!hit || !el.contains(hit)) return { error: "перекрыт: " + (hit ? hit.tagName + "." + hit.className : "ничем") };
    return { x, y };
  })()`);
  if (point.error) throw new Error(`${selector}: ${point.error}`);
  const { x, y } = point;
  await page("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await page("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

// Ввод в текстовое поле: щелчок, «выделить все» и вставка текста вместо выделенного (или Backspace для пустого).
async function type(selector, text) {
  await click(selector);
  await page("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", modifiers: 2, commands: ["selectAll"] });
  await page("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA", modifiers: 2 });
  if (text) {
    await page("Input.insertText", { text });
  } else {
    await page("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8, commands: ["deleteBackward"] });
    await page("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
  }
}

const field = (scenario, i) => `#${scenario.id} .field[data-index="${i}"]`;
const shownText = (scenario) => ev(`document.querySelector("#${scenario.id} .prompt").textContent`);

// Состояние элементов управления сценария, как в cases.json: options — индекс, ввод и область; chips — отмеченные.
const readControls = (scenario) => ev(`[...document.querySelectorAll("#${scenario.id} .field[data-index]")].map((field) => {
  if (field.querySelector(".chips")) return { picked: [...field.querySelectorAll("input:checked")].map((i) => i.value) };
  const input = field.querySelector(".extra-input"), choice = field.querySelector(".extra-choice input:checked");
  return { index: Number(field.querySelector(".select-trigger").dataset.value), input: input ? input.value : "", choice: choice ? choice.value : "" };
})`);

async function openScenario(scenario) {
  await click(`.rail a[data-id="${scenario.id}"]`);
  await waitFor(() => ev(`!document.getElementById("${scenario.id}").hidden`), `сценарий ${scenario.id} не показан`);
}

async function choose(scenario, i, index) {
  const f = field(scenario, i);
  await click(`${f} .select-trigger`);
  const pop = show(`${f} .listbox-pop`);
  await waitFor(() => ev(`document.querySelector(${pop}).matches(":popover-open")`), `список ${f} не открылся`);
  await click(`${f} .option[data-value="${index}"]`, { scroll: false });
  await waitFor(() => ev(`!document.querySelector(${pop}).matches(":popover-open")`), `список ${f} не закрылся`);
}

// Приводит элементы управления к состояниям случая теми же действиями, что и пользователь.
async function applyStates(scenario, states) {
  for (const [i, param] of scenario.params.entries()) {
    const want = states[i];
    let have = (await readControls(scenario))[i];
    const f = field(scenario, i);
    if (param.choices) {
      for (const [k, name] of param.choices.entries()) {
        if (want.picked.includes(name) !== have.picked.includes(name)) await click(`${f} .chips > .chip:nth-child(${k + 1})`);
      }
      continue;
    }
    if (want.index !== have.index) {
      await choose(scenario, i, want.index);
      have = (await readControls(scenario))[i];
    }
    const option = param.option[want.index];
    if (option.input !== undefined && want.input !== have.input) await type(`${f} .extra-input`, want.input);
    if (option.choice && want.choice && want.choice !== have.choice) {
      await click(`${f} .extra-choice .seg:nth-child(${option.choice.indexOf(want.choice) + 1})`);
    }
  }
}

const clipboard = () => ev("navigator.clipboard.readText()", { awaitPromise: true, userGesture: true });
const setClipboard = (text) => ev(`navigator.clipboard.writeText(${show(text)})`, { awaitPromise: true, userGesture: true });

async function copy(scenario) {
  await setClipboard(SENTINEL);
  await click(`#${scenario.id} [data-action="copy"]`);
  return waitFor(async () => { const text = await clipboard(); return text !== SENTINEL && text; }, "буфер не изменился");
}

async function download(scenario) {
  const dir = mkdtempSync(join(temp, "download-"));
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dir, eventsEnabled: true });
  const before = downloads.length;
  await click(`#${scenario.id} [data-action="download"]`);
  const item = await waitFor(() => downloads.slice(before).find((d) => d.state === "completed"), "скачивание не завершилось");
  const files = readdirSync(dir);
  if (!(files.length === 1 && files[0] === item.suggestedFilename)) throw new Error(`в каталоге скачивания ${show(files)}, ожидался ${show(item.suggestedFilename)}`);
  return { name: item.suggestedFilename, text: readFileSync(join(dir, item.suggestedFilename), "utf8") };
}

// --- Проверки сценариев -----------------------------------------------------------------------------------

await page("Page.navigate", { url: `${origin}/` });
await waitFor(() => ev(`document.readyState === "complete"`), "страница не загрузилась", 20_000);

const loaded = await ev(`document.documentElement.classList.contains("js")`);
if (!loaded) die(`страница не запустила скрипты (нет класса js на html)${pageErrors.length ? `:\n  ${pageErrors.join("\n  ")}` : ""}`);
const data = JSON.parse(await ev(`document.getElementById("data").textContent`));
const scenarios = data.scenarios;
const sectionIds = await ev(`[...document.querySelectorAll(".scenario")].map((s) => s.id)`);
ok(show(sectionIds) === show(scenarios.map((s) => s.id)), "страница", `разделы ${show(sectionIds)} не совпадают со сценариями данных`);

const isDefault = (param, state) => param.choices
  ? show([...state.picked].sort()) === show(param.default === "all" ? [...param.choices].sort() : [])
  : state.index === 0 && !state.input && !state.choice;
const defaultStates = (scenario) => scenario.params.map((param) => param.choices
  ? { picked: param.default === "all" ? [...param.choices] : [] }
  : { index: 0, input: "", choice: "" });
const typeable = (states) => states.every((s) => !/[\n\r]/.test(s.input ?? "")); // <input> не хранит переводы строк

// Исходный HTML без выполнения скриптов: запасной вариант страницы без JS показывает тот же текст.
const staticTexts = await ev(`fetch(location.href).then((r) => r.text()).then((html) => {
  const doc = new DOMParser().parseFromString(html, "text/html");
  return Object.fromEntries([...doc.querySelectorAll(".scenario")].map((s) => [s.id, s.querySelector(".prompt").textContent]));
})`, { awaitPromise: true });

let driven = 0, skipped = 0;
for (const scenario of scenarios) {
  const where = `${scenario.id}`;
  try {
    const own = cases.filter((c) => c.id === scenario.id);
    const base = own.find((c) => c.states.length === scenario.params.length && c.states.every((s, i) => isDefault(scenario.params[i], s)));
    if (!ok(base, where, "в cases.json нет случая «все по умолчанию»")) continue;
    const expected = base.expected;
    const reset = scenario.params.length ? `#${scenario.id} [data-action="reset"]` : null;

    await openScenario(scenario);
    sameText(await shownText(scenario), expected, where, "начальный текст на странице");
    sameText(staticTexts[scenario.id] ?? "", expected, where, "текст в исходном HTML");
    if (reset) ok(await ev(`document.querySelector(${show(reset)}).disabled`), where, "кнопка сброса не отключена при значениях по умолчанию");
    sameText(await copy(scenario), expected, where, "начальный текст в буфере");
    const file = await download(scenario);
    const num = await ev(`document.querySelector("#${scenario.id} h2 .num").textContent`);
    ok(file.name === `${num}-${scenario.id}.md`, where, `имя скачанного файла ${show(file.name)}, ожидалось ${show(`${num}-${scenario.id}.md`)}`);
    ok(file.name === scenario.file, where, `имя скачанного файла ${show(file.name)} не совпадает с данными страницы ${show(scenario.file)}`);
    sameText(file.text, expected, where, "скачанный текст при значениях по умолчанию");

    // Все случаи, которые можно воспроизвести вводом: смена вариантов, ввод, область, чипы, сочетания.
    for (const item of own) {
      if (!typeable(item.states)) { skipped += 1; continue; }
      driven += 1;
      await applyStates(scenario, item.states);
      const label = `${where} ${show(item.states).slice(0, 160)}`;
      const shown = await shownText(scenario);
      sameText(shown, item.expected, label, "текст на странице");
      if (!item.expected.includes(data.missing)) sameText(await copy(scenario), item.expected, label, "текст в буфере");
    }

    // Пустое обязательное поле: копирование и скачивание заблокированы, поле подсвечено.
    for (const [i, param] of scenario.params.entries()) {
      const index = param.option?.findIndex((o) => o.input !== undefined) ?? -1;
      if (index < 0) continue;
      const blockedLabel = `${where} параметр «${param.key}»`;
      await applyStates(scenario, defaultStates(scenario).map((s, k) => k === i ? { index, input: "", choice: "" } : s));
      await setClipboard(SENTINEL);
      const filesBefore = downloads.length;
      await click(`#${scenario.id} [data-action="copy"]`);
      const marked = await ev(`(() => {
        const field = document.querySelector(${show(field(scenario, i))}), input = field.querySelector(".extra-input");
        return { invalid: input.getAttribute("aria-invalid"), hidden: input.hidden, note: !field.querySelector(".note").hidden,
          focused: document.activeElement === input,
          toast: [...document.querySelectorAll(".toast.warn")].some((t) => t.textContent.includes(${show(`Заполните поле «${param.key}»`)})) };
      })()`);
      // Рамка подсвечена цветом предупреждения (переход цвета длится доли секунды).
      const warned = await waitFor(() => ev(`(() => {
        const probe = Object.assign(document.createElement("i"), { style: "color: var(--warn)" });
        document.body.append(probe);
        const warn = getComputedStyle(probe).color;
        probe.remove();
        return getComputedStyle(document.querySelector(${show(`${field(scenario, i)} .extra-input`)})).borderTopColor === warn;
      })()`), "рамка поля не окрасилась").catch(() => false);
      ok(marked.invalid === "true" && !marked.hidden, blockedLabel, "пустое обязательное поле не помечено aria-invalid");
      ok(marked.note, blockedLabel, "нет подсказки у пустого обязательного поля");
      ok(marked.focused, blockedLabel, "фокус не переведен на пустое обязательное поле");
      ok(marked.toast, blockedLabel, "нет уведомления о пустом поле");
      ok(warned, blockedLabel, "рамка пустого обязательного поля не окрашена цветом предупреждения");
      await click(`#${scenario.id} [data-action="download"]`);
      await sleep(QUIET);
      ok((await clipboard()) === SENTINEL, blockedLabel, "копирование не заблокировано: буфер изменился");
      ok(downloads.length === filesBefore, blockedLabel, "скачивание не заблокировано: файл скачан");
    }

    // Сброс: умолчания, очищенный ввод, чипы в исходном состоянии, кнопка снова недоступна.
    if (reset) {
      const last = own.filter((c) => typeable(c.states)).at(-1);
      await applyStates(scenario, last.states);
      ok(!(await ev(`document.querySelector(${show(reset)}).disabled`)), where, "кнопка сброса отключена при измененных значениях");
      await click(reset);
      sameText(await shownText(scenario), expected, where, "текст после сброса");
      const controls = await readControls(scenario);
      scenario.params.forEach((param, i) => {
        const now = controls[i];
        if (param.choices) {
          ok(show([...now.picked].sort()) === show(param.default === "all" ? [...param.choices].sort() : []), where,
            `чипы «${param.key}» после сброса: отмечено ${show(now.picked)}`);
        } else {
          ok(now.index === 0 && now.input === "", where, `параметр «${param.key}» после сброса: ${show(now)}`);
        }
      });
      ok(await ev(`document.querySelector(${show(reset)}).disabled`), where, "кнопка сброса не отключена после сброса");
      sameText(await copy(scenario), expected, where, "текст в буфере после сброса");
      // Очищенный ввод виден и пользователю: вариант с вводом снова пуст и требует значения.
      for (const [i, param] of scenario.params.entries()) {
        const index = param.option?.findIndex((o) => o.input !== undefined) ?? -1;
        if (index < 0) continue;
        await choose(scenario, i, index);
        const again = (await readControls(scenario))[i];
        ok(again.input === "", where, `ввод «${param.key}» после сброса и повторного выбора не пуст`);
        const area = param.option[index].choice;
        if (area) ok(again.choice === area[0], where, `область «${param.key}» после сброса и повторного выбора: ${show(again.choice)}, ожидалось ${show(area[0])}`);
        ok((await shownText(scenario)).includes(data.missing), where, `текст после сброса и повторного выбора «${param.key}» без пометки о пустом вводе`);
        await click(reset);
      }
    }

    // Копирование и скачивание при измененных значениях: ровно показанный текст.
    const contrast = own.filter((c) => typeable(c.states)).at(-1);
    await applyStates(scenario, contrast.states);
    const shownNow = await shownText(scenario);
    if (!contrast.expected.includes(data.missing)) {
      sameText(await copy(scenario), shownNow, where, "в буфере не тот текст, что показан");
      const changed = await download(scenario);
      sameText(changed.text, shownNow, where, "в скачанном файле не тот текст, что показан");
      ok(changed.name === scenario.file, where, `имя файла ${show(changed.name)} при измененных значениях, ожидалось ${show(scenario.file)}`);
    }
  } catch (error) {
    fault(where, `тест прерван: ${error.message}`);
  }
}
ok(driven > 0, "случаи", "не воспроизведено ни одного случая из cases.json");

ok(pageErrors.length === 0, "консоль", `ошибки на странице:\n    ${pageErrors.join("\n    ")}`);

const seconds = ((Date.now() - started) / 1000).toFixed(1);
if (failures.length) {
  console.error(`smoke: ПРОВАЛ, ${failures.length} из ${checks} проверок:`);
  for (const message of failures.slice(0, SHOWN)) console.error(`  ${message}`);
  if (failures.length > SHOWN) console.error(`  ... и еще ${failures.length - SHOWN}`);
  console.error(`smoke: ${seconds} с`);
  finish(1);
} else {
  console.log(`smoke: страница работает: проверок ${checks}, сценариев ${scenarios.length}, случаев воспроизведено ${driven} (пропущено с переводом строки во вводе: ${skipped}), ${seconds} с`);
  finish(0);
}
