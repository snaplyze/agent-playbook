// Smoke-тест собранной страницы в headless Chrome: страница работает так, как её видит пользователь. Без зависимостей:
// Node 22+ (WebSocket встроен) и Chrome DevTools Protocol. Запуск, как в CI (<tmp> — каталог вне репозитория):
//   python3 scripts/build.py --check --site <tmp>/site --parity-cases <tmp>/cases.json
//   node scripts/smoke.mjs <tmp>/site/index.html <tmp>/cases.json
// Аргументы: страница (файл или каталог с index.html) и случаи для сверки. Chrome: CHROME_BIN, иначе первый найденный
// из google-chrome, google-chrome-stable, chromium, chromium-browser.
//
// Что проверяется (всё через настоящие события мыши и клавиатуры, как у пользователя): начальный текст каждого
// сценария (<pre>, исходный HTML и то, что копируется) равен ожидаемому тексту случая «все по умолчанию»; выбор
// варианта в комбобоксе, ввод с лишними пробелами, снятие и установка чипов дают текст случая из
// cases.json; пустое обязательное поле блокирует копирование и скачивание и подсвечивается; копирование кладет в
// буфер ровно показанный текст, скачивание дает файл NN-имя.md с тем же текстом; сброс возвращает умолчания,
// очищает ввод и восстанавливает чипы; на странице нет исключений и ошибок консоли; страница не делает внешних запросов
// (любой запрос не к локальному серверу — провал; data: и blob: сетью не считаются) и не нарушает свою политику CSP
// (событие securitypolicyviolation и записи журнала), а политика в ней есть и не разрешает небезопасного.
//
// Страница отдается локальным сервером на 127.0.0.1; внешние запросы блокируются и записываются: тест не зависит от
// сети. Анимации выключены эмуляцией prefers-reduced-motion: клики попадают в уже разложенные элементы.
//
// Копирование без запасного пути не работает там, где нет Clipboard API, поэтому проверяются оба сбоя: API нет совсем
// (удален до загрузки страницы) и запись отклонена; в обоих текст выделен, подсказка показана, а Ctrl+C кладет в буфер
// ровно показанный текст. Предел длины промпта (limit в данных страницы): сверх него у счетчика пометка в обеих темах и
// одно объявление для чтения с экрана, в пределах пометки нет, копирование не блокируется.
//
// Windows: системный буфер там хранит переводы строк как CRLF, поэтому текст из буфера приводится к LF (текст <pre> и
// скачанный файл сравниваются как есть), а дерево процессов Chrome завершается через taskkill.
import { spawn } from "node:child_process";
import { accessSync, constants, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync } from "node:fs";
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
const IS_WINDOWS = process.platform === "win32";

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

// Код выхода задается первым делом: что бы ни случилось при очистке, он не станет нулевым при провале.
// Очистки идут по очереди, в обратном порядке регистрации (Chrome, сервер, каталоги), и не мешают друг другу.
async function finish(code) {
  process.exitCode = code;
  if (finishing) return;
  finishing = true;
  const timer = setTimeout(() => process.exit(code), 15_000); // зависшая очистка не держит процесс
  for (const cleanup of cleanups.splice(0).reverse()) {
    try {
      await cleanup();
    } catch (error) {
      console.error(`smoke: очистка не удалась: ${error?.message ?? error}`);
    }
  }
  clearTimeout(timer);
  process.exit(code);
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
  const gone = new Promise((done) => {
    child.on("exit", (code, signal) => { exited = { code, signal }; done(); });
    child.on("error", (error) => { exited = { code: null, signal: null, error }; done(); });
  });
  // Очистка знает о Chrome сразу после запуска: сбой на старте тоже завершает его до удаления профиля.
  cleanups.push(async () => {
    await killTree(child);
    await Promise.race([gone, sleep(5_000)]); // профиль удаляется только после выхода Chrome
  });
  const portFile = join(profile, "DevToolsActivePort");
  const until = Date.now() + 20_000;
  while (Date.now() < until) {
    if (exited) break;
    let text = "";
    try {
      text = readFileSync(portFile, "utf8");
    } catch {
      // Файла еще нет или Chrome его пишет (на Windows чтение дает EBUSY): ждем дальше.
    }
    const [port, path] = text.split("\n");
    if (port && path) return { child, gone, wsUrl: `ws://127.0.0.1:${port}${path}` };
    await sleep(40);
  }
  const early = Boolean(exited && !exited.error); // завершился сам, не успев открыть порт: признак отказа песочницы
  const why = exited ? `завершился сразу (код ${exited.code ?? exited.signal ?? exited.error?.message})` : "не открыл порт за 20 с";
  await killTree(child);
  await Promise.race([gone, sleep(3_000)]);
  return { error: `Chrome ${why}${stderr.trim() ? `:\n${stderr.trim()}` : ""}`, early };
}

// Завершает Chrome вместе со вспомогательными процессами и ждет, пока это произойдет: до тех пор профиль занят.
async function killTree(child) {
  if (IS_WINDOWS) {
    // Групп процессов нет: taskkill завершает дерево по идентификатору корня.
    await new Promise((done) => spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true })
      .on("error", done).on("exit", done));
    return;
  }
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
// Профиль Chrome может оставаться занятым доли секунды после выхода процессов (на Windows это EBUSY): удаление с повторами.
cleanups.push(() => rmSync(temp, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 }));

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
if (launched.error && !launched.early) die(launched.error);
if (launched.error) {
  // Без песочницы: на машинах, где пользовательские пространства имен запрещены (контейнеры, запуск от root),
  // Chrome с песочницей завершается сразу после запуска. Только в этом случае (не при таймауте и не при ошибке
  // запуска файла) повтор без песочницы допустим: страница своя и статичная.
  const retry = await launch(bin, ["--no-sandbox"], join(temp, "profile-no-sandbox"));
  if (retry.error) die(`${launched.error}\nповтор с --no-sandbox:\n${retry.error}`);
  console.error("smoke: Chrome с песочницей не стартовал, запущен с --no-sandbox");
  launched = retry;
}

const cdp = await Cdp.connect(launched.wsUrl);
// Штатное закрытие идет до завершения процессов: только после Browser.close Chrome удаляет свой каталог
// com.google.Chrome.* во временной папке системы, а после сигналов каталог остается.
cleanups.push(async () => {
  await Promise.race([cdp.send("Browser.close").catch(() => {}), sleep(3_000)]);
  await Promise.race([launched.gone, sleep(3_000)]);
});
const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
const { sessionId: sid } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
const page = (method, params) => cdp.send(method, params, sid);

// Ошибки страницы: необработанные исключения, console.error/assert и записи журнала уровня error.
// Внешние запросы: все, кроме локального сервера и адресов без сети (data:, blob:, about:).
const pageErrors = [];
const externalRequests = [];
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
    const url = params.request.url;
    const local = url.startsWith(`${origin}/`) || /^(?:data|blob|about):/.test(url);
    if (!local) externalRequests.push(url);
    (local
      ? page("Fetch.continueRequest", { requestId: params.requestId })
      : page("Fetch.failRequest", { requestId: params.requestId, errorReason: "BlockedByClient" }))
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
// Нарушения CSP страницы: событие securitypolicyviolation собирается с самого начала загрузки документа.
await page("Page.addScriptToEvaluateOnNewDocument", { source: `window.__violations = [];
  document.addEventListener("securitypolicyviolation", (e) => window.__violations.push(e.violatedDirective + " " + (e.blockedURI || e.sample || "")), true);` });
// Страница по адресу /?no-clipboard открывается без Clipboard API: он удален до выполнения ее скриптов, как в небезопасном контексте.
await page("Page.addScriptToEvaluateOnNewDocument", { source: `if (location.search === "?no-clipboard") delete Navigator.prototype.clipboard;` });
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
const highlighted = (scenario) => ev(`[...document.querySelectorAll("#${scenario.id} .prompt .p")].map((el) => el.textContent)`);
const shownText = (scenario) => ev(`document.querySelector("#${scenario.id} .prompt").textContent`);

// Состояние элементов управления сценария, как в cases.json: options — индекс и ввод; chips — отмеченные.
const readControls = (scenario) => ev(`[...document.querySelectorAll("#${scenario.id} .field[data-index]")].map((field) => {
  if (field.querySelector(".chips")) return { picked: [...field.querySelectorAll("input:checked")].map((i) => i.value) };
  const input = field.querySelector(".extra-input");
  return { index: Number(field.querySelector(".select-trigger").dataset.value), input: input ? input.value : "" };
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
  }
}

// Текст из системного буфера: конвенция платформы (CRLF на Windows) не дефект страницы, остальное сравнивается как есть.
const fromSystem = (text) => IS_WINDOWS ? text.replace(/\r\n/g, "\n") : text;
const clipboard = async () => fromSystem(await ev("navigator.clipboard.readText()", { awaitPromise: true, userGesture: true }));
const setClipboard = (text) => ev(`navigator.clipboard.writeText(${show(text)})`, { awaitPromise: true, userGesture: true });

async function copy(scenario) {
  await setClipboard(SENTINEL);
  // Прежние выделение и уведомления не должны скрыть запасной путь, сработавший при успешной записи.
  await ev(`getSelection().removeAllRanges(); document.querySelectorAll(".toast").forEach((t) => t.remove())`);
  await click(`#${scenario.id} [data-action="copy"]`);
  const text = await waitFor(async () => { const text = await clipboard(); return text !== SENTINEL && text; }, "буфер не изменился");
  // Запись удалась: кнопка «Скопировано», выделения и подсказки про Ctrl+C нет (запасной путь молчит).
  const after = await waitFor(() => ev(`(() => {
    const label = document.querySelector("#${scenario.id} [data-action='copy'] .btn-label").textContent;
    if (label === "Скопировать промпт") return null;
    return { label, collapsed: getSelection().isCollapsed, hint: [...document.querySelectorAll(".toast.warn")].some((t) => t.textContent.includes("Ctrl+C")) };
  })()`), "кнопка не показала результат копирования");
  const where = `${scenario.id} копирование`;
  ok(after.label === "Скопировано", where, `метка кнопки после успешной записи ${show(after.label)}`);
  ok(after.collapsed, where, "после успешной записи текст выделен");
  ok(!after.hint, where, "после успешной записи показана подсказка про Ctrl+C");
  return text;
}

async function download(scenario) {
  const dir = mkdtempSync(join(temp, "download-"));
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dir, eventsEnabled: true });
  const before = downloads.length;
  await click(`#${scenario.id} [data-action="download"]`);
  const item = await waitFor(() => downloads.slice(before).find((d) => d.state === "completed"), "скачивание не завершилось");
  // Скачивания страницы считаются по событиям браузера, а не по каталогу: Chrome на Windows иногда кладет туда
  // свой служебный downloads.htm, который страница не скачивала.
  const started = downloads.slice(before);
  if (started.length !== 1) throw new Error(`скачиваний ${started.length}, ожидалось одно: ${show(started.map((d) => `${d.suggestedFilename} ← ${d.url}`))}`);
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

// Состояния по умолчанию берутся из данных страницы; что они совпадают с показанным в начале, проверяют начальный текст
// и текст после сброса.
const normal = (state) => state.picked ? { picked: [...state.picked].sort() } : { index: state.index, input: state.input || "" };
const defaultStates = (scenario) => scenario.defaults.map(normal);
const isDefault = (scenario, states) => show(states.map(normal)) === show(defaultStates(scenario));
const fileName = async (scenario) => `${await ev(`document.querySelector("#${scenario.id} h2 .num").textContent`)}-${scenario.id}.md`;
const typeable = (states) => states.every((s) => !/[\n\r]/.test(s.input ?? "")); // <input> не хранит переводы строк

// Исходный HTML без выполнения скриптов: запасной вариант страницы без JS показывает тот же текст.
// Политика страницы запрещает fetch, поэтому разбирается тот же файл, который отдал сервер.
const staticTexts = await ev(`(() => {
  const doc = new DOMParser().parseFromString(${show(readText(pagePath))}, "text/html");
  return Object.fromEntries([...doc.querySelectorAll(".scenario")].map((s) => [s.id, s.querySelector(".prompt").textContent]));
})()`);
const policy = await ev(`document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content ?? ""`);
ok(/default-src 'none'/.test(policy) && /script-src 'sha256-/.test(policy) && !/unsafe-/.test(policy), "страница", `нет политики CSP без небезопасных источников: ${show(policy)}`);

let driven = 0, skipped = 0;
for (const scenario of scenarios) {
  const where = `${scenario.id}`;
  try {
    const own = cases.filter((c) => c.id === scenario.id);
    const base = own.find((c) => c.states.length === scenario.params.length && isDefault(scenario, c.states));
    if (!ok(base, where, "в cases.json нет случая «все по умолчанию»")) continue;
    const expected = base.expected;
    const reset = scenario.params.length ? `#${scenario.id} [data-action="reset"]` : null;

    await openScenario(scenario);
    sameText(await shownText(scenario), expected, where, "начальный текст на странице");
    sameText(staticTexts[scenario.id] ?? "", expected, where, "текст в исходном HTML");
    if (reset) ok(await ev(`document.querySelector(${show(reset)}).disabled`), where, "кнопка сброса не отключена при значениях по умолчанию");
    sameText(await copy(scenario), expected, where, "начальный текст в буфере");
    const file = await download(scenario);
    const name = await fileName(scenario);
    ok(file.name === name, where, `имя скачанного файла ${show(file.name)}, ожидалось ${show(name)}`);
    sameText(file.text, expected, where, "скачанный текст при значениях по умолчанию");

    // Все случаи, которые можно воспроизвести вводом: смена вариантов, ввод, чипы, сочетания.
    for (const item of own) {
      if (!typeable(item.states)) { skipped += 1; continue; }
      driven += 1;
      await applyStates(scenario, item.states);
      const label = `${where} ${show(item.states).slice(0, 160)}`;
      const shown = await shownText(scenario);
      sameText(shown, item.expected, label, "текст на странице");
      // Подсвечены ровно строки параметров, подряд, в порядке параметров.
      const marked = await highlighted(scenario);
      ok(marked.length === scenario.params.length && marked.every((line, k) => line.startsWith(`- ${scenario.params[k].key}: `)) &&
        (!marked.length || shown.includes(marked.join("\n"))), label, `подсвечены не строки параметров: ${show(marked).slice(0, 200)}`);
      if (!item.expected.includes(data.missing)) sameText(await copy(scenario), item.expected, label, "текст в буфере");
    }

    // Пустое обязательное поле: копирование и скачивание заблокированы, поле подсвечено.
    for (const [i, param] of scenario.params.entries()) {
      const index = param.option?.findIndex((o) => o.input !== undefined) ?? -1;
      if (index < 0) continue;
      const blockedLabel = `${where} параметр «${param.key}»`;
      await applyStates(scenario, defaultStates(scenario).map((s, k) => k === i ? { index, input: "" } : s));
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
          ok(show(normal(now)) === show(defaultStates(scenario)[i]), where,
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
      ok(changed.name === await fileName(scenario), where, `имя файла ${show(changed.name)} при измененных значениях, ожидалось ${show(await fileName(scenario))}`);
    }
  } catch (error) {
    fault(where, `тест прерван: ${error.message}`);
  }
}
ok(driven > 0, "случаи", "не воспроизведено ни одного случая из cases.json");

// --- Запасной путь копирования -------------------------------------------------------------------------------

// Нарушения CSP собираются по каждому документу: перед уходом со страницы они переносятся в общий список.
const violations = [];
const takeViolations = async () => violations.push(...await ev("window.__violations"));
const pressCopy = async () => {
  await page("Input.dispatchKeyEvent", { type: "keyDown", key: "c", code: "KeyC", modifiers: 2, commands: ["copy"] });
  await page("Input.dispatchKeyEvent", { type: "keyUp", key: "c", code: "KeyC", modifiers: 2 });
};
async function loadPage(path) {
  await takeViolations();
  await page("Page.navigate", { url: `${origin}${path}` });
  await waitFor(() => ev(`location.search === ${show(path.slice(1))} && document.readyState === "complete" && document.documentElement.classList.contains("js")`), `страница ${path} не загрузилась`, 20_000);
}

// Клик «Скопировать» там, где запись в буфер не удалась: текст выделен целиком, подсказка показана, исключений нет;
// после этого Ctrl+C кладет в буфер ровно показанный текст. Читать буфер можно только со страницы с API: вызывает сам случай.
async function fallbackCopy(scenario, where) {
  const errors = pageErrors.length;
  await click(`#${scenario.id} [data-action="copy"]`);
  const state = await waitFor(() => ev(`(() => {
    const label = document.querySelector("#${scenario.id} [data-action='copy'] .btn-label").textContent;
    if (label !== "Выделено") return null;
    return { selected: getSelection().toString(), shown: document.querySelector("#${scenario.id} .prompt").textContent,
      hint: [...document.querySelectorAll(".toasts .toast.warn")].some((t) => t.textContent.includes("Ctrl+C")) };
  })()`), "после клика нет выделения и подсказки");
  ok(state.hint, where, "нет подсказки про Ctrl+C в области уведомлений");
  sameText(state.selected, state.shown, where, "выделенный текст не совпадает с показанным (завершающий перевод строки)");
  ok(pageErrors.length === errors, where, `при копировании возникли ошибки: ${pageErrors.slice(errors).join("; ")}`);
  await pressCopy();
  return state.shown;
}

try {
  const scenario = scenarios[0];
  await openScenario(scenario);
  await applyStates(scenario, defaultStates(scenario)); // прежние проверки могли оставить пустое обязательное поле
  // Запись отклонена: Clipboard API есть, но writeText отвергает вызов (запрет разрешения).
  await setClipboard(SENTINEL);
  await ev(`navigator.clipboard.writeText = () => Promise.reject(new DOMException("denied", "NotAllowedError"))`);
  let shown;
  try {
    shown = await fallbackCopy(scenario, "копирование при отказе записи");
  } finally {
    await ev(`delete navigator.clipboard.writeText`);
  }
  sameText(await waitFor(async () => { const text = await clipboard(); return text !== SENTINEL && text; }, "Ctrl+C не изменил буфер"), shown, "копирование при отказе записи", "Ctrl+C после выделения: в буфере не показанный текст");

  // Clipboard API нет совсем.
  await setClipboard(SENTINEL);
  await loadPage("/?no-clipboard");
  ok(await ev(`navigator.clipboard === undefined`), "копирование без Clipboard API", "в странице остался navigator.clipboard");
  await openScenario(scenario);
  const bare = await fallbackCopy(scenario, "копирование без Clipboard API");
  await loadPage("/");
  sameText(await waitFor(async () => { const text = await clipboard(); return text !== SENTINEL && text; }, "Ctrl+C не изменил буфер"), bare, "копирование без Clipboard API", "Ctrl+C после выделения: в буфере не показанный текст");
} catch (error) {
  fault("запасной путь копирования", `тест прерван: ${error.message}`);
}

// --- Предел длины промпта ---------------------------------------------------------------------------------------

try {
  const limit = data.limit;
  ok(Number.isInteger(limit) && limit > 0, "предел длины", `в данных страницы нет предела: ${show(limit)}`);
  const scenario = scenarios.find((s) => s.params.some((p) => p.option?.some((o) => o.input !== undefined)));
  const i = scenario.params.findIndex((p) => p.option?.some((o) => o.input !== undefined));
  const index = scenario.params[i].option.findIndex((o) => o.input !== undefined);
  const where = `предел длины ${scenario.id}`;
  const input = `${field(scenario, i)} .extra-input`;
  const reset = `#${scenario.id} [data-action="reset"]`;
  // Счетчик: пометка, текст с пределом и цвета предупреждения; цвета палитры читаются из переменных CSS темы.
  const counter = () => ev(`(() => {
    const meta = document.querySelector("#${scenario.id} .meta");
    const probe = Object.assign(document.createElement("i"), { style: "color: var(--warn); background-color: var(--warn-wash)" });
    document.body.append(probe);
    const want = getComputedStyle(probe);
    const result = { over: meta.classList.contains("is-over"), text: meta.textContent, tinted: getComputedStyle(meta).color === want.color && getComputedStyle(meta).backgroundColor === want.backgroundColor };
    probe.remove();
    return result;
  })()`);
  // Объявления считаются событиями, а не по тостам в DOM: тост живет несколько секунд и переживает фазы теста.
  await ev(`window.__notices = 0;
    new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
      if (n.classList?.contains("warn") && n.textContent.includes("длиннее")) window.__notices += 1;
    }))).observe(document.querySelector(".toasts"), { childList: true })`);
  const announced = async (phase, expected, action) => {
    await ev(`window.__notices = 0`);
    await action();
    const got = await ev(`window.__notices`);
    ok(got === expected, `${where} ${phase}`, `объявлений о превышении ${got}, ожидалось ${expected} (одно при каждом пересечении предела, без повтора на каждый символ)`);
  };
  const exceed = async (extra) => {
    await type(input, "а".repeat(limit + 100 + extra));
    return waitFor(async () => { const c = await counter(); return c.over && c; }, "пометка о превышении предела не появилась");
  };
  const within = async () => {
    await type(input, "а");
    return waitFor(async () => { const c = await counter(); return !c.over && c; }, "пометка осталась при тексте в пределах");
  };
  const digits = (text) => text.replace(/\D/g, "");

  await openScenario(scenario);
  await applyStates(scenario, defaultStates(scenario));
  ok(!(await counter()).over, where, "пометка есть у промпта по умолчанию");
  await choose(scenario, i, index);
  for (const scheme of ["light", "dark"]) {
    await page("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }, { name: "prefers-color-scheme", value: scheme }] });
    // Светлая фаза пересекает предел (одно объявление); в темной текст остается сверх предела (объявлений нет).
    let long;
    await announced(scheme, scheme === "light" ? 1 : 0, async () => { long = await exceed(scheme === "dark" ? 1 : 0); });
    ok(digits(long.text).endsWith(String(limit)), `${where} (${scheme})`, `в пометке нет предела ${limit}: ${show(long.text)}`);
    ok(long.tinted, `${where} (${scheme})`, "пометка не окрашена цветами предупреждения (--warn, --warn-wash)");
    ok((await shownText(scenario)).length > limit, where, "текст не превышает предел: проверка не проверяет превышение");
  }
  await page("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  ok(await ev(`document.querySelector(".toasts").matches('[role="status"][aria-live="polite"]')`), where, "область уведомлений не объявляется для чтения с экрана");
  // Копирование и скачивание не заблокированы.
  const longText = await shownText(scenario);
  sameText(await copy(scenario), longText, where, "в буфере не тот текст, что показан, при превышении");
  sameText((await download(scenario)).text, longText, where, "в файле не тот текст, что показан, при превышении");
  // Возврат в пределы снимает пометку и не объявляется; новое превышение объявляется снова.
  let short;
  await announced("возврат в предел", 0, async () => { short = await within(); });
  ok(!/больше/.test(short.text) && !short.tinted, where, `в пределах счетчик похож на пометку: ${show(short.text)}`);
  ok((await shownText(scenario)).length <= limit, where, "текст в пределах длиннее предела");
  await announced("повторное превышение", 1, () => exceed(2));
  // То же после сброса: он возвращает в предел, а следующее превышение снова объявляется.
  await announced("сброс", 0, async () => {
    await click(reset);
    await waitFor(async () => !(await counter()).over, "пометка осталась после сброса");
  });
  await choose(scenario, i, index);
  await announced("превышение после сброса", 1, () => exceed(3));
  await click(reset);
} catch (error) {
  fault("предел длины", `тест прерван: ${error.message}`);
}
await takeViolations();

ok(externalRequests.length === 0, "сеть", `внешние запросы со страницы:\n    ${externalRequests.join("\n    ")}`);
ok(violations.length === 0, "CSP", `нарушения политики страницы:\n    ${violations.join("\n    ")}`);
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
