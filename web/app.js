const state = {
  config: null,
  page: 14,
  language: "da",
  zoom: 1,
  request: 0,
};
const PDF_PAGE_WIDTH = 552.756;
const PAGE_NUMBER_OFFSET = 2;

const elements = {
  pages: [
    {
      container: document.querySelector("#pdf-page-left"),
      image: document.querySelector("#pdf-image-left"),
      overlays: document.querySelector("#overlays-left"),
    },
    {
      container: document.querySelector("#pdf-page-right"),
      image: document.querySelector("#pdf-image-right"),
      overlays: document.querySelector("#overlays-right"),
    },
  ],
  page: document.querySelector("#page-number"),
  total: document.querySelector("#page-total"),
  previous: document.querySelector("#previous"),
  next: document.querySelector("#next"),
  languageControl: document.querySelector(".language-control"),
  languageToggle: document.querySelector("#language-toggle"),
  languageOptions: document.querySelector("#language-options"),
  languageCode: document.querySelector("#language-code"),
  languageName: document.querySelector("#language-name"),
  exportPdf: document.querySelector("#export-pdf"),
  status: document.querySelector("#status"),
  spread: document.querySelector("#spread"),
  template: document.querySelector("#entry-template"),
  titleTemplate: document.querySelector("#title-template"),
};

function clampPage(value) {
  const page = Number.parseInt(value, 10);
  if (Number.isNaN(page)) return state.page;
  return Math.min(state.config.last_page, Math.max(state.config.first_page, page));
}

function spreadStart(value) {
  const page = clampPage(value);
  const offset = page - state.config.first_page;
  return state.config.first_page + Math.floor(offset / 2) * 2;
}

function displayedPage(pdfPage) {
  return pdfPage - PAGE_NUMBER_OFFSET;
}

function pdfPage(displayedPageNumber) {
  return Number.parseInt(displayedPageNumber, 10) + PAGE_NUMBER_OFFSET;
}

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  if (isError) console.error(message);
}

function setLanguageMenuOpen(open) {
  elements.languageOptions.hidden = !open;
  elements.languageToggle.setAttribute("aria-expanded", String(open));
}

function updateLanguagePicker() {
  const selected = state.config.languages.find(({ code }) => code === state.language);
  elements.languageCode.textContent = state.language.toUpperCase();
  elements.languageName.textContent = selected?.name || state.language;
  elements.exportPdf.setAttribute("aria-label", `导出 ${selected?.name || state.language} PDF`);
  elements.exportPdf.title = `导出 ${selected?.name || state.language} PDF`;
  for (const option of elements.languageOptions.querySelectorAll(".language-option")) {
    option.setAttribute("aria-checked", String(option.dataset.language === state.language));
  }
}

async function exportCurrentLanguage() {
  const selected = state.config.languages.find(({ code }) => code === state.language);
  elements.exportPdf.disabled = true;
  elements.exportPdf.setAttribute("aria-busy", "true");
  setStatus(`正在生成 ${selected?.name || state.language} PDF…`);
  try {
    const response = await fetch(`/api/export/${encodeURIComponent(state.language)}.pdf`);
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `illustrated-dictionary-${state.language}.pdf`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    setStatus(`${selected?.name || state.language} PDF 已导出`);
  } catch (error) {
    setStatus(`导出失败：${error.message}`, true);
  } finally {
    elements.exportPdf.disabled = false;
    elements.exportPdf.removeAttribute("aria-busy");
  }
}

function fitSpreadToWindow() {
  state.zoom = Math.min(1, window.innerWidth / (PDF_PAGE_WIDTH * 2));
  elements.spread.style.setProperty("--zoom", state.zoom);
}

function configureAudio(button, url, label) {
  button.disabled = !url;
  button.setAttribute("aria-label", url ? label : `${label}（音频尚未生成）`);
  if (!url) return;
  button.addEventListener("click", async () => {
    button.classList.add("playing");
    const audio = new Audio(url);
    audio.addEventListener("ended", () => button.classList.remove("playing"), { once: true });
    audio.addEventListener("error", () => button.classList.remove("playing"), { once: true });
    await audio.play();
  });
}

function overlapArea(first, second) {
  const width = Math.max(0, Math.min(first.right, second.right) - Math.max(first.left, second.left));
  const height = Math.max(0, Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top));
  return width * height;
}

function overlapPenalty(first, second) {
  return overlapArea(first, second);
}

function padded(rectangle, padding) {
  return {
    left: rectangle.left - padding,
    top: rectangle.top - padding,
    right: rectangle.right + padding,
    bottom: rectangle.bottom + padding,
  };
}

function candidatePositions(source, width, height, pageWidth, pageHeight) {
  const verticalStep = height + 1;
  const horizontalStep = 12 * state.zoom;
  const positions = [];
  const add = (left, top) => {
    if (left < 0 || top < 0 || left + width > pageWidth || top + height > pageHeight) return;
    const key = `${Math.round(left * 10)}:${Math.round(top * 10)}`;
    if (!positions.some((position) => position.key === key)) {
      positions.push({ key, left, top, right: left + width, bottom: top + height });
    }
  };

  for (let row = 0; row <= 7; row += 1) {
    const below = source.bottom + state.zoom + row * verticalStep;
    const above = source.top - height - 1 - row * verticalStep;
    for (let column = 0; column <= 5; column += 1) {
      const shifts = column === 0 ? [0] : [-column * horizontalStep, column * horizontalStep];
      for (const shift of shifts) {
        add(source.left + shift, below);
        add(source.right - width + shift, below);
        add(source.left + shift, above);
        add(source.right - width + shift, above);
      }
    }
  }

  add(source.right + 1, source.top);
  add(source.left - width - 1, source.top);
  return positions;
}

function layoutTranslations(overlays) {
  const pageRectangle = overlays.getBoundingClientRect();
  if (!pageRectangle.width || !pageRectangle.height) return;

  const titleItems = [...overlays.querySelectorAll(".title-overlay")].map((overlay) => {
    const source = overlay.getBoundingClientRect();
    const tag = overlay.querySelector(".title-translation");
    const translation = tag.getBoundingClientRect();
    return {
      tag,
      source: {
        left: source.left - pageRectangle.left,
        top: source.top - pageRectangle.top,
        right: source.right - pageRectangle.left,
        bottom: source.bottom - pageRectangle.top,
      },
      translation: {
        left: translation.left - pageRectangle.left,
        top: translation.top - pageRectangle.top,
        right: translation.right - pageRectangle.left,
        bottom: translation.bottom - pageRectangle.top,
      },
    };
  });
  const items = [...overlays.querySelectorAll(".entry-overlay")].map((overlay) => {
    const source = overlay.getBoundingClientRect();
    const tag = overlay.querySelector(".translation-tag");
    tag.style.transform = "none";
    return {
      overlay,
      tag,
      source: {
        left: source.left - pageRectangle.left,
        top: source.top - pageRectangle.top,
        right: source.right - pageRectangle.left,
        bottom: source.bottom - pageRectangle.top,
      },
    };
  });

  const sourceRectangles = [
    ...titleItems.map(({ source }) => source),
    ...items.map(({ source }) => ({
      ...source,
      top: Math.min(source.bottom, source.top + 3 * state.zoom),
    })),
  ];
  const numberRectangles = items.map(({ source }) => ({
    left: Math.max(0, source.left - 10 * state.zoom),
    top: Math.min(source.bottom, source.top + 3 * state.zoom),
    right: source.left,
    bottom: source.bottom,
  }));
  const placed = titleItems.map(({ translation }) => padded(translation, 0.5));
  const ordered = [...items].sort((first, second) => {
    const widthDifference = second.tag.offsetWidth - first.tag.offsetWidth;
    return widthDifference || first.source.top - second.source.top || first.source.left - second.source.left;
  });

  for (const item of ordered) {
    const width = item.tag.offsetWidth;
    const height = item.tag.offsetHeight;
    const sourceWidth = item.source.right - item.source.left;
    const wraps = item.tag.classList.contains("wrap-translation");
    const startsAtNumber = wraps || width > sourceWidth + 5 * state.zoom;
    const numberOffset = (wraps ? 15 : 10) * state.zoom;
    const anchor = {
      ...item.source,
      left: startsAtNumber
        ? Math.max(0, item.source.left - numberOffset)
        : item.source.left,
    };
    const preferredTop = anchor.bottom + state.zoom;
    const candidates = candidatePositions(
      anchor,
      width,
      height,
      pageRectangle.width,
      pageRectangle.height,
    );
    let best = null;
    let bestCollision = Number.POSITIVE_INFINITY;
    let bestPlacementCost = Number.POSITIVE_INFINITY;

    for (const candidate of candidates) {
      const protectedCandidate = padded(candidate, 0.5);
      const sourceCollision = sourceRectangles.reduce(
        (total, source) => total + overlapPenalty(candidate, source),
        0,
      );
      const numberCollision = numberRectangles.reduce(
        (total, number) => total + overlapPenalty(candidate, number),
        0,
      );
      const translationCollision = placed.reduce(
        (total, translation) => total + overlapPenalty(protectedCandidate, translation),
        0,
      );
      const horizontalDistance = Math.abs(candidate.left - anchor.left);
      const verticalDistance = Math.abs(candidate.top - preferredTop);
      const collision = sourceCollision + numberCollision + translationCollision;
      const placementCost = horizontalDistance + verticalDistance * 12;
      const collisionIsBetter = collision < bestCollision;
      const collisionIsEqual = collision === bestCollision;
      if (
        collisionIsBetter
        || (collisionIsEqual && placementCost < bestPlacementCost)
      ) {
        best = candidate;
        bestCollision = collision;
        bestPlacementCost = placementCost;
      }
      if (
        sourceCollision === 0
        && numberCollision === 0
        && translationCollision === 0
        && horizontalDistance <= 1
        && verticalDistance <= 1
      ) break;
    }

    if (!best) continue;
    item.tag.style.transform = `translate(${best.left - item.source.left}px, ${best.top - item.source.bottom}px)`;
    placed.push(padded(best, 0.5));
  }
}

function layoutVisiblePages() {
  for (const page of elements.pages) {
    if (!page.container.hidden) layoutTranslations(page.overlays);
  }
}

function renderEntries(entries, overlays) {
  let located = 0;
  const renderedPositions = new Set();
  for (const entry of entries) {
    if (!entry.bbox) continue;
    const positionKey = [
      entry.bbox.left,
      entry.bbox.top,
      entry.bbox.right,
      entry.bbox.bottom,
      entry.translation,
      entry.translation_noun_marker,
    ].join(":");
    if (renderedPositions.has(positionKey)) continue;
    renderedPositions.add(positionKey);
    located += 1;
    const fragment = elements.template.content.cloneNode(true);
    const overlay = fragment.querySelector(".entry-overlay");
    const sourceAudioHit = fragment.querySelector(".source-audio-hit");
    const translationTag = fragment.querySelector(".translation-tag");
    const translation = fragment.querySelector(".translation-text");
    const nounMarker = fragment.querySelector(".translation-noun-marker");

    overlay.style.setProperty("--left", `${entry.bbox.left * 100}%`);
    overlay.style.setProperty("--top", `${entry.bbox.top * 100}%`);
    overlay.style.setProperty("--right", `${entry.bbox.right * 100}%`);
    overlay.style.setProperty("--bottom", `${entry.bbox.bottom * 100}%`);
    overlay.dataset.entryId = entry.id;
    translation.textContent = entry.translation;
    nounMarker.textContent = entry.translation_noun_marker || "";
    nounMarker.hidden = !entry.translation_noun_marker;
    const translationWords = entry.translation.trim().split(/\s+/);
    if (translationWords.length > 1 && entry.translation.length > 24) {
      translationTag.classList.add("wrap-translation");
    }
    configureAudio(sourceAudioHit, entry.source_audio, `播放 ${entry.source_text}`);
    configureAudio(translationTag, entry.translation_audio, `播放 ${entry.translation}`);
    overlays.append(fragment);
  }
  return located;
}

function translatedHeading(text) {
  return text.replace(/^\s*\d+(?:\.\d+)*\s+/, "").trim();
}

function renderHeading(bbox, text, overlays, className = "") {
  if (!bbox || !text) return;
  const fragment = elements.titleTemplate.content.cloneNode(true);
  const overlay = fragment.querySelector(".title-overlay");
  const translation = fragment.querySelector(".title-translation");
  if (className) {
    overlay.classList.add(`${className}-overlay`);
    translation.classList.add(`${className}-translation`);
  }
  overlay.style.setProperty("--left", `${bbox.left * 100}%`);
  overlay.style.setProperty("--top", `${bbox.top * 100}%`);
  overlay.style.setProperty("--right", `${bbox.right * 100}%`);
  overlay.style.setProperty("--bottom", `${bbox.bottom * 100}%`);
  translation.textContent = translatedHeading(text);
  overlays.append(fragment);
}

function renderTitle(data, overlays) {
  if (data.language === "en") return;
  renderHeading(data.title_bbox, data.title, overlays);
}

function renderSections(data, overlays) {
  if (data.language === "en") return;
  for (const section of data.sections || []) {
    renderHeading(section.bbox, section.title, overlays, "section");
  }
}

function renderPage(data, overlays) {
  overlays.replaceChildren();
  renderTitle(data, overlays);
  renderSections(data, overlays);
  return renderEntries(data.entries, overlays);
}

function loadImage(image, pageNumber) {
  return new Promise((resolve, reject) => {
    image.onload = resolve;
    image.onerror = reject;
    image.src = `/api/pdf/pages/${pageNumber}.png?scale=2`;
  });
}

async function loadSpread(nextPage = state.page) {
  const request = ++state.request;
  state.page = spreadStart(nextPage);
  const pageNumbers = [state.page, state.page + 1].filter(
    (page) => page <= state.config.last_page,
  );
  const displayedPageNumbers = pageNumbers.map(displayedPage);
  elements.page.value = displayedPageNumbers[0];
  elements.previous.disabled = state.page <= state.config.first_page;
  elements.next.disabled = state.page + 1 >= state.config.last_page;
  elements.total.textContent = pageNumbers.length === 2
    ? `– ${displayedPageNumbers[1]} / ${displayedPage(state.config.last_page)}`
    : `/ ${displayedPage(state.config.last_page)}`;
  setStatus(`正在载入第 ${displayedPageNumbers.join("–")} 页…`);

  try {
    const results = await Promise.all(pageNumbers.map(async (pageNumber, index) => {
      const response = await fetch(
        `/api/pages/${pageNumber}?language=${encodeURIComponent(state.language)}`,
      );
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      await loadImage(elements.pages[index].image, pageNumber);
      return data;
    }));
    if (request !== state.request) return;
    let entryCount = 0;
    let locatedCount = 0;
    results.forEach((data, index) => {
      elements.pages[index].container.hidden = false;
      entryCount += data.entries.length;
      locatedCount += renderPage(data, elements.pages[index].overlays);
    });
    for (let index = results.length; index < elements.pages.length; index += 1) {
      elements.pages[index].container.hidden = true;
    }
    setStatus(`${displayedPageNumbers.join("–")} · ${entryCount} 个词条 · ${locatedCount} 个位置标注`);
    requestAnimationFrame(layoutVisiblePages);
    history.replaceState(null, "", `?page=${displayedPage(state.page)}&language=${state.language}`);
  } catch (error) {
    if (request === state.request) setStatus(`载入失败：${error.message}`, true);
  }
}

async function initialize() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error(await response.text());
  state.config = await response.json();
  const params = new URLSearchParams(location.search);
  const requestedLanguage = params.get("language");
  const translationLanguages = state.config.languages.filter(({ code }) => code !== "en");
  if (!translationLanguages.length) {
    throw new Error("没有可用的翻译语言");
  }
  const languageCodes = translationLanguages.map(({ code }) => code);
  state.language = languageCodes.includes(requestedLanguage) ? requestedLanguage : (languageCodes.includes("da") ? "da" : languageCodes[0]);
  const requestedPage = params.get("page");
  state.page = spreadStart(
    requestedPage === null ? state.config.first_page : pdfPage(requestedPage),
  );

  for (const language of translationLanguages) {
    const option = document.createElement("button");
    option.className = "language-option";
    option.type = "button";
    option.role = "menuitemradio";
    option.dataset.language = language.code;
    option.textContent = language.name;
    option.addEventListener("click", () => {
      state.language = language.code;
      setLanguageMenuOpen(false);
      updateLanguagePicker();
      loadSpread();
    });
    elements.languageOptions.append(option);
  }
  updateLanguagePicker();
  elements.page.min = displayedPage(state.config.first_page);
  elements.page.max = displayedPage(state.config.last_page);
  fitSpreadToWindow();
  await loadSpread();
}

elements.previous.addEventListener("click", () => loadSpread(state.page - 2));
elements.next.addEventListener("click", () => loadSpread(state.page + 2));
elements.page.addEventListener("change", () => loadSpread(pdfPage(elements.page.value)));
elements.page.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadSpread(pdfPage(elements.page.value));
});
elements.languageToggle.addEventListener("click", () => {
  setLanguageMenuOpen(elements.languageOptions.hidden);
});
elements.exportPdf.addEventListener("click", exportCurrentLanguage);
document.addEventListener("click", (event) => {
  if (!elements.languageControl.contains(event.target)) setLanguageMenuOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.languageOptions.hidden) {
    setLanguageMenuOpen(false);
    elements.languageToggle.focus();
    return;
  }
  if (event.target.matches("input") || !elements.languageOptions.hidden) return;
  if (event.key === "ArrowLeft") {
    loadSpread(state.page - 2);
  }
  if (event.key === "ArrowRight") {
    loadSpread(state.page + 2);
  }
});
let resizeFrame = null;
window.addEventListener("resize", () => {
  if (resizeFrame) cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    fitSpreadToWindow();
    layoutVisiblePages();
    resizeFrame = null;
  });
});

initialize().catch((error) => setStatus(`初始化失败：${error.message}`, true));
