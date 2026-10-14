(() => {
  const root = document.documentElement;
  root.classList.add("js");
  const { scenarios: data, missing } = JSON.parse(document.getElementById("data").textContent);
  const { clean, model: modelOf, compose } = playbook;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = matchMedia("(max-width: 640px)");
  const coarse = matchMedia("(pointer: coarse)");
  const stacked = matchMedia("(max-width: 960px)");
  const popoverApi = "showPopover" in HTMLElement.prototype;
  const esc = (value) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const plural = (n) => n % 10 === 1 && n % 100 !== 11 ? "символ" : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? "символа" : "символов";
  const count = (n) => n.toLocaleString("ru-RU") + " " + plural(n);
  const replay = (el, cls) => { el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); };

  // --- Тема: системная, светлая или тёмная; выбор хранится в localStorage. Цвет theme-color — фон страницы
  // (--paper) выбранной темы: единственный источник цветов — CSS.
  const themeButtons = [...document.querySelectorAll("[data-theme-set]")];
  function applyTheme(value) {
    if (value) root.dataset.theme = value; else delete root.dataset.theme;
    const paper = getComputedStyle(root).getPropertyValue("--paper").trim();
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", paper));
    themeButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === (value || ""))));
  }
  themeButtons.forEach((b) => b.addEventListener("click", () => {
    applyTheme(b.dataset.themeSet);
    try { b.dataset.themeSet ? localStorage.setItem("theme", b.dataset.themeSet) : localStorage.removeItem("theme"); } catch {}
  }));
  applyTheme(root.dataset.theme || "");

  // --- Уведомления: контейнер role=status живёт в DOM постоянно, фокус не переводится,
  // исчезают сами (факт уже в буфере обмена или в файле), пауза при наведении, клик закрывает.
  const toasts = document.querySelector(".toasts");
  const TOAST_ICON = {
    ok: '<svg class="icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M6.5 10.3l2.4 2.4L13.6 8"/></svg>',
    warn: '<svg class="icon" viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M10 6.5v4.2M10 13.6v.2"/></svg>'
  };
  function toast(kind, text, life = 4200) {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.style.setProperty("--life", life + "ms");
    el.innerHTML = TOAST_ICON[kind] + "<span></span>";
    el.lastElementChild.textContent = text;
    const leave = () => {
      if (el.classList.contains("is-leaving")) return;
      el.classList.add("is-leaving");
      el.addEventListener("animationend", (e) => { if (e.animationName === "toast-out") el.remove(); });
      setTimeout(() => el.remove(), 600);
    };
    el.addEventListener("animationend", (e) => { if (e.animationName === "toast-life") leave(); });
    el.addEventListener("click", leave);
    while (toasts.children.length >= 3) toasts.firstElementChild.remove();
    toasts.append(el);
  }

  // --- Кастомный select: WAI-ARIA APG «Select-Only Combobox» (фокус остаётся на кнопке,
  // aria-activedescendant указывает на подсвеченный пункт). Список — popover в верхнем слое;
  // на узких экранах и при сенсорном вводе — нижняя шторка.
  function makeSelect(field, param, onChange) {
    const trigger = field.querySelector(".select-trigger");
    const pop = field.querySelector(".listbox-pop");
    const options = [...pop.querySelectorAll('[role="option"]')];
    const valueEl = trigger.querySelector(".select-value");
    const labels = param.option.map((o) => o.label.toLowerCase());
    let current = Number(trigger.dataset.value), active = current, typed = "", typeTimer = 0;
    const isOpen = () => popoverApi ? pop.matches(":popover-open") : pop.classList.contains("is-open");

    function place() {
      const sheet = narrow.matches || coarse.matches;
      pop.classList.toggle("sheet", sheet);
      pop.style.cssText = "";
      if (sheet) return;
      // Размеры списка задает CSS: предел высоты (--list-max), высота пункта и рамка с отступами — для оценки
      // высоты, пока список скрыт и scrollHeight равен нулю.
      const css = getComputedStyle(pop);
      const px = (value) => parseFloat(value);
      const frame = px(css.paddingTop) + px(css.paddingBottom) + px(css.borderTopWidth) + px(css.borderBottomWidth);
      const r = trigger.getBoundingClientRect();
      const need = Math.min(pop.scrollHeight || options.length * px(getComputedStyle(options[0]).minHeight) + frame, px(css.getPropertyValue("--list-max")));
      const below = innerHeight - r.bottom - 12, above = r.top - 12;
      const up = below < need && above > below;
      pop.dataset.side = up ? "top" : "bottom";
      pop.style.left = r.left + "px";
      pop.style.width = r.width + "px";
      pop.style.setProperty("--space", (up ? above : below) + "px");
      if (up) pop.style.bottom = (innerHeight - r.top + 6) + "px"; else pop.style.top = (r.bottom + 6) + "px";
    }
    function setActive(i, scroll) {
      active = Math.max(0, Math.min(options.length - 1, i));
      options.forEach((o, k) => o.classList.toggle("is-active", k === active));
      trigger.setAttribute("aria-activedescendant", options[active].id);
      if (scroll) options[active].scrollIntoView({ block: "nearest" });
    }
    const onScroll = (e) => { if (!(e.target instanceof Node && pop.contains(e.target))) place(); };
    function opened(on) {
      trigger.setAttribute("aria-expanded", String(on));
      if (on) {
        place();
        setActive(active, true);
        addEventListener("scroll", onScroll, { capture: true, passive: true });
        addEventListener("resize", place);
      } else {
        trigger.removeAttribute("aria-activedescendant");
        removeEventListener("scroll", onScroll, { capture: true });
        removeEventListener("resize", place);
      }
    }
    const outside = (e) => { if (!pop.contains(e.target) && !trigger.contains(e.target)) closeList(); };
    function openList(focusIndex) {
      if (isOpen()) { if (focusIndex !== undefined) setActive(focusIndex, true); return; }
      active = focusIndex === undefined ? current : focusIndex;
      if (popoverApi) pop.showPopover();
      else { pop.classList.add("is-open"); opened(true); document.addEventListener("pointerdown", outside, true); }
    }
    function closeList() {
      if (!isOpen()) return;
      if (popoverApi) pop.hidePopover();
      else { pop.classList.remove("is-open"); opened(false); document.removeEventListener("pointerdown", outside, true); }
    }
    function set(i) {
      if (i === current) return false;
      current = i;
      trigger.dataset.value = String(i);
      valueEl.textContent = param.option[i].label;
      options.forEach((o, k) => o.setAttribute("aria-selected", String(k === i)));
      return true;
    }
    function commit(i) { const changed = set(i); closeList(); if (changed) onChange(); }
    if (popoverApi) {
      pop.addEventListener("beforetoggle", (e) => { if (e.newState === "open") place(); });
      pop.addEventListener("toggle", (e) => opened(e.newState === "open"));
    } else {
      trigger.addEventListener("click", () => isOpen() ? closeList() : openList());
    }
    trigger.addEventListener("keydown", (e) => {
      const k = e.key, open = isOpen();
      switch (k) {
        case "ArrowDown": e.preventDefault(); return open ? setActive(active + 1, true) : openList();
        case "ArrowUp":
          e.preventDefault();
          if (!open) return openList(0);
          return e.altKey ? commit(active) : setActive(active - 1, true);
        case "Enter": case " ": e.preventDefault(); return open ? commit(active) : openList();
        case "Tab": if (open) { commit(active); if (document.activeElement !== trigger) e.preventDefault(); } return;
        case "Escape": if (open) { e.preventDefault(); closeList(); } return;
        case "Home": e.preventDefault(); return open ? setActive(0, true) : openList(0);
        case "End": e.preventDefault(); return open ? setActive(options.length - 1, true) : openList(options.length - 1);
        case "PageUp": if (open) { e.preventDefault(); setActive(active - 10, true); } return;
        case "PageDown": if (open) { e.preventDefault(); setActive(active + 10, true); } return;
      }
      if (k.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
      e.preventDefault();
      clearTimeout(typeTimer);
      typed += k.toLowerCase();
      typeTimer = setTimeout(() => { typed = ""; }, 500);
      const same = typed.length > 1 && [...typed].every((c) => c === typed[0]);
      const needle = same ? typed[0] : typed;
      const start = same || typed.length === 1 ? active + 1 : active;
      for (let n = 0; n < labels.length; n++) {
        const i = (start + n) % labels.length;
        if (labels[i].startsWith(needle)) { open ? setActive(i, true) : openList(i); break; }
      }
    });
    trigger.addEventListener("keyup", (e) => { if (e.key === " ") e.preventDefault(); });
    options.forEach((o, i) => {
      o.addEventListener("pointermove", () => { if (i !== active) setActive(i); });
      o.addEventListener("click", () => { trigger.focus({ preventScroll: true }); commit(i); });
    });
    return { set, value: () => current };
  }

  // --- Переключение сценариев: один виден, остальные скрыты; переход через View Transitions, если есть.
  const sections = [...document.querySelectorAll(".scenario")];
  const links = [...document.querySelectorAll(".rail a[data-id]")];
  const rail = document.querySelector(".rail ul");
  function moveIndicator() {
    const link = rail.querySelector('a[aria-current="page"]');
    if (!link) return;
    rail.style.setProperty("--ind-top", link.offsetTop + "px");
    rail.style.setProperty("--ind-h", link.offsetHeight + "px");
    if (stacked.matches) rail.scrollTo({ left: link.offsetLeft - (rail.clientWidth - link.offsetWidth) / 2, behavior: reduced.matches ? "auto" : "smooth" });
  }
  function show(id, scroll) {
    sections.forEach((section) => { section.hidden = section.id !== id; });
    links.forEach((link) => link.dataset.id === id ? link.setAttribute("aria-current", "page") : link.removeAttribute("aria-current"));
    moveIndicator();
    const section = document.getElementById(id);
    if (scroll && section.getBoundingClientRect().top < 0) section.scrollIntoView({ block: "start" });
  }
  function go(id, scroll) {
    if (!document.getElementById(id).hidden) return show(id, scroll);
    const run = () => show(id, scroll);
    if (document.startViewTransition && !reduced.matches) document.startViewTransition(run); else run();
  }
  const current = () => sections.some((s) => s.id === location.hash.slice(1)) ? location.hash.slice(1) : sections[0].id;
  links.forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    history.replaceState(null, "", "#" + link.dataset.id);
    go(link.dataset.id, true);
  }));
  addEventListener("hashchange", () => go(current(), true));
  show(current(), false);
  // Браузер прокрутил к якорю до того, как скрипт спрятал остальные промпты: выравниваем заново.
  if (location.hash) document.getElementById(current()).scrollIntoView({ block: "start" });
  addEventListener("resize", moveIndicator);
  if (document.fonts) document.fonts.ready.then(moveIndicator);

  // Подсветка: строки параметров — по их месту в тексте (range.first и далее, по одной на параметр).
  function highlight(text, range, changed) {
    return text.split("\n").map((line, n) => {
      let out = esc(line);
      if (/^#{1,6} /.test(line)) return '<span class="h">' + out + "</span>";
      out = out.replace(/^(\s*)(- |\d+\. )/, '$1<span class="m">$2</span>');
      out = out.replace(/`([^`]+)`/g, '<span class="c">`$1`</span>');
      out = out.replace(/\*\*(.+?)\*\*/g, '<span class="b">**$1**</span>');
      const param = n - range.first;
      if (param >= 0 && param < range.count) out = '<span class="p' + (changed.includes(param) ? " changed" : "") + '">' + out + "</span>";
      return out;
    }).join("\n");
  }

  sections.forEach((section) => {
    const pre = section.querySelector(".prompt");
    // Текст по умолчанию — то, что собрала сборка и показывает <pre> до первой отрисовки.
    const defaultText = pre.textContent;
    const model = modelOf(data.find((item) => item.id === section.id), defaultText);
    const meta = section.querySelector(".meta");
    const bar = section.querySelector(".bar");
    const resetButton = section.querySelector('[data-action="reset"]');
    const fields = [...section.querySelectorAll(".field[data-index]")];
    const selects = new Map();
    const touched = new Set(); // поля, у которых пустое значение уже показано подсказкой
    let text = "", previous = null;

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(([entry]) => bar.classList.toggle("is-stuck", !entry.isIntersecting && entry.boundingClientRect.top < 0))
        .observe(section.querySelector(".bar-sentinel"));
    }
    fields.forEach((field) => {
      const param = model.params[field.dataset.index];
      if (param.choices) return;
      selects.set(field, makeSelect(field, param, () => {
        // Другой вариант — другая область: выбор области начинается с умолчания.
        sync(field, { ...read(field), choice: "" });
        render();
        const input = field.querySelector(".extra-input:not([hidden])");
        if (input && !clean(input.value)) input.focus();
      }));
    });

    // Чтение состояния элементов управления, как в model.defaults и в states у compose; DOM не меняет.
    function read(field) {
      const param = model.params[field.dataset.index];
      if (param.choices) return { picked: [...field.querySelectorAll("input:checked")].map((input) => input.value) };
      const input = field.querySelector(".extra-input");
      const choice = field.querySelector(".extra-choice");
      const radio = choice && !choice.hidden && choice.querySelector("input:checked");
      return { index: selects.get(field).value(), input: input ? input.value : "", choice: radio ? radio.value : "" };
    }
    // Синхронизация: приводит элементы управления и то, что из них следует (видимость поля значения и области,
    // подсказка, aria-invalid), к состоянию. Повторный вызов с тем же состоянием ничего не меняет.
    function sync(field, state) {
      const param = model.params[field.dataset.index];
      if (param.choices) {
        field.querySelectorAll("input").forEach((input) => { input.checked = state.picked.includes(input.value); });
        return;
      }
      selects.get(field).set(state.index);
      const option = param.option[state.index];
      const input = field.querySelector(".extra-input");
      const choice = field.querySelector(".extra-choice");
      const extra = field.querySelector(".extra");
      if (input) {
        const value = state.input || "";
        const need = option.input !== undefined;
        const empty = need && !clean(value);
        if (input.value !== value) input.value = value;
        input.hidden = !need;
        input.placeholder = option.input || "";
        input.setAttribute("aria-invalid", String(empty));
        field.querySelector(".note").hidden = !(empty && touched.has(field));
      }
      if (choice) {
        choice.hidden = !option.choice;
        if (option.choice) {
          const built = [...choice.querySelectorAll("input")].map((radio) => radio.value);
          if (built.length !== option.choice.length || built.some((value, i) => value !== option.choice[i])) {
            choice.innerHTML = option.choice.map((value) => '<label class="seg"><input type="radio" name="' + field.querySelector(".select-trigger").id +
              '-choice" value="' + esc(value) + '"><span>' + esc(value) + "</span></label>").join("");
          }
          choice.querySelectorAll("input").forEach((radio) => { radio.checked = radio.value === (state.choice || option.choice[0]); });
        }
      }
      if (extra) extra.dataset.open = String(Boolean(input && !input.hidden || choice && !choice.hidden));
    }
    function render() {
      const states = fields.map(read);
      fields.forEach((field, i) => sync(field, states[i]));
      const result = compose(model, states, missing);
      const lines = result.lines;
      text = result.text;
      const changed = previous ? lines.map((line, i) => line !== previous[i] ? i : -1).filter((i) => i >= 0) : [];
      previous = lines;
      pre.innerHTML = highlight(text, { first: result.first, count: lines.length }, changed);
      meta.textContent = count(text.length);
      changed.forEach((i) => replay(fields[i], "is-pulse"));
      if (resetButton) resetButton.disabled = text === defaultText;
    }
    section.addEventListener("input", (event) => {
      if (event.target.matches(".extra-input")) touched.add(event.target.closest(".field"));
      render();
    });
    fields.forEach((field) => field.addEventListener("animationend", (e) => { if (e.animationName === "label-pulse") field.classList.remove("is-pulse"); }));
    render();

    // Пустое обязательное значение: не отдаём промпт с меткой «не указано», а ведём к полю.
    function blocked() {
      const input = [...section.querySelectorAll(".extra-input:not([hidden])")].find((candidate) => !clean(candidate.value));
      if (!input) return false;
      const field = input.closest(".field");
      touched.add(field);
      render();
      replay(input, "is-shake");
      toast("warn", "Заполните поле «" + field.querySelector(".label").textContent + "»");
      input.focus();
      return true;
    }
    function done(button, label, kind, message) {
      const text = button.querySelector(".btn-label");
      const original = button.dataset.label || (button.dataset.label = text.textContent);
      text.textContent = label;
      button.classList.toggle("is-done", kind === "ok");
      clearTimeout(button.timer);
      button.timer = setTimeout(() => { text.textContent = original; button.classList.remove("is-done"); }, 2200);
      toast(kind, message);
    }
    section.querySelector('[data-action="copy"]').addEventListener("click", (event) => {
      const button = event.currentTarget;
      if (blocked()) return;
      // writeText вызывается в обработчике клика: пока жива пользовательская активация.
      navigator.clipboard.writeText(text).then(
        () => done(button, "Скопировано", "ok", "Промпт скопирован: " + count(text.length)),
        () => {
          const range = document.createRange();
          range.selectNodeContents(pre);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          done(button, "Выделено", "warn", "Буфер обмена недоступен. Текст выделен: нажмите Ctrl+C");
        });
    });
    section.querySelector('[data-action="download"]').addEventListener("click", (event) => {
      if (blocked()) return;
      const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: section.dataset.file });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      done(event.currentTarget, "Скачано", "ok", "Файл " + section.dataset.file + " скачан");
    });
    if (resetButton) resetButton.addEventListener("click", () => {
      touched.clear();
      fields.forEach((field, i) => sync(field, model.defaults[i]));
      render();
      toast("ok", "Параметры сброшены");
    });
  });
})();
