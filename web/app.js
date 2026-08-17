const state = {
  config: null,
  page: 14,
  language: "da",
  zoom: 1,
  request: 0,
};

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
  language: document.querySelector("#language"),
  title: document.querySelector("#page-title"),
  status: document.querySelector("#status"),
  stage: document.querySelector("#page-stage"),
  spread: document.querySelector("#spread"),
  template: document.querySelector("#entry-template"),
  titleTemplate: document.querySelector("#title-template"),
  zoomIn: document.querySelector("#zoom-in"),
  zoomOut: document.querySelector("#zoom-out"),
  zoomValue: document.querySelector("#zoom-value"),
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

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.closest(".reader-note").classList.toggle("error", isError);
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
  const allowedArea = 8 * state.zoom * state.zoom;
  return Math.max(0, overlapArea(first, second) - allowedArea);
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
  const horizontalStep = Math.max(width * 0.55, 12);
  const positions = [];
  const add = (left, top) => {
    if (left < 0 || top < 0 || left + width > pageWidth || top + height > pageHeight) return;
    const key = `${Math.round(left * 10)}:${Math.round(top * 10)}`;
    if (!positions.some((position) => position.key === key)) {
      positions.push({ key, left, top, right: left + width, bottom: top + height });
    }
  };

  for (let row = 0; row <= 7; row += 1) {
    const below = source.bottom + 1 + row * verticalStep;
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
    tag.style.transform = "none";
    return {
      tag,
      source: {
        left: source.left - pageRectangle.left,
        top: source.top - pageRectangle.top,
        right: source.right - pageRectangle.left,
        bottom: source.bottom - pageRectangle.top,
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

  const sourceRectangles = [...titleItems, ...items].map(({ source }) => source);
  const placed = titleItems.map(({ source, tag }) => padded({
    left: source.left,
    top: source.bottom,
    right: source.left + tag.offsetWidth,
    bottom: source.bottom + tag.offsetHeight,
  }, 0.5));
  const ordered = [...items].sort((first, second) => {
    const widthDifference = second.tag.offsetWidth - first.tag.offsetWidth;
    return widthDifference || first.source.top - second.source.top || first.source.left - second.source.left;
  });

  for (const item of ordered) {
    const width = item.tag.offsetWidth;
    const height = item.tag.offsetHeight;
    const candidates = candidatePositions(
      item.source,
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
        (total, source) => total + overlapPenalty(protectedCandidate, source),
        0,
      );
      const translationCollision = placed.reduce(
        (total, translation) => total + overlapPenalty(protectedCandidate, translation),
        0,
      );
      const horizontalDistance = Math.abs(candidate.left - item.source.left);
      const verticalDistance = Math.abs(candidate.top - item.source.bottom);
      const collision = sourceCollision + translationCollision;
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
    ].join(":");
    if (renderedPositions.has(positionKey)) continue;
    renderedPositions.add(positionKey);
    located += 1;
    const fragment = elements.template.content.cloneNode(true);
    const overlay = fragment.querySelector(".entry-overlay");
    const sourceAudioHit = fragment.querySelector(".source-audio-hit");
    const translationTag = fragment.querySelector(".translation-tag");
    const translation = fragment.querySelector(".translation-text");

    overlay.style.setProperty("--left", `${entry.bbox.left * 100}%`);
    overlay.style.setProperty("--top", `${entry.bbox.top * 100}%`);
    overlay.style.setProperty("--right", `${entry.bbox.right * 100}%`);
    overlay.style.setProperty("--bottom", `${entry.bbox.bottom * 100}%`);
    overlay.dataset.entryId = entry.id;
    translation.textContent = entry.translation;
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

function renderTitle(data, overlays) {
  if (!data.title_bbox || !data.title || data.language === "en") return;
  const fragment = elements.titleTemplate.content.cloneNode(true);
  const overlay = fragment.querySelector(".title-overlay");
  const translation = fragment.querySelector(".title-translation");
  overlay.style.setProperty("--left", `${data.title_bbox.left * 100}%`);
  overlay.style.setProperty("--top", `${data.title_bbox.top * 100}%`);
  overlay.style.setProperty("--right", `${data.title_bbox.right * 100}%`);
  overlay.style.setProperty("--bottom", `${data.title_bbox.bottom * 100}%`);
  translation.textContent = data.title;
  overlays.append(fragment);
}

function renderPage(data, overlays) {
  overlays.replaceChildren();
  renderTitle(data, overlays);
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
  elements.page.value = state.page;
  elements.previous.disabled = state.page <= state.config.first_page;
  elements.next.disabled = state.page + 1 >= state.config.last_page;
  elements.total.textContent = pageNumbers.length === 2
    ? `– ${pageNumbers[1]} / ${state.config.last_page}`
    : `/ ${state.config.last_page}`;
  setStatus(`正在载入第 ${pageNumbers.join("–")} 页…`);
  elements.spread.classList.add("loading");

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
    elements.title.textContent = results.map((data) => data.title).filter(Boolean).join(" · ")
      || `第 ${pageNumbers.join("–")} 页`;
    setStatus(`${pageNumbers.join("–")} · ${entryCount} 个词条 · ${locatedCount} 个位置标注`);
    requestAnimationFrame(layoutVisiblePages);
    history.replaceState(null, "", `?page=${state.page}&language=${state.language}`);
  } catch (error) {
    if (request === state.request) setStatus(`载入失败：${error.message}`, true);
  } finally {
    if (request === state.request) elements.spread.classList.remove("loading");
  }
}

function setZoom(value) {
  state.zoom = Math.min(2, Math.max(0.7, value));
  elements.spread.style.setProperty("--zoom", state.zoom);
  elements.zoomValue.value = `${Math.round(state.zoom * 100)}%`;
  elements.zoomOut.disabled = state.zoom <= 0.7;
  elements.zoomIn.disabled = state.zoom >= 2;
  requestAnimationFrame(layoutVisiblePages);
}

async function initialize() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error(await response.text());
  state.config = await response.json();
  const params = new URLSearchParams(location.search);
  const requestedLanguage = params.get("language");
  const languageCodes = state.config.languages.map(({ code }) => code);
  state.language = languageCodes.includes(requestedLanguage) ? requestedLanguage : (languageCodes.includes("da") ? "da" : languageCodes[0]);
  state.page = spreadStart(params.get("page") || state.config.first_page);

  for (const language of state.config.languages) {
    const option = new Option(language.name, language.code, false, language.code === state.language);
    elements.language.add(option);
  }
  elements.page.min = state.config.first_page;
  elements.page.max = state.config.last_page;
  setZoom(1);
  await loadSpread();
}

elements.previous.addEventListener("click", () => loadSpread(state.page - 2));
elements.next.addEventListener("click", () => loadSpread(state.page + 2));
elements.page.addEventListener("change", () => loadSpread(elements.page.value));
elements.page.addEventListener("keydown", (event) => {
  if (event.key === "Enter") loadSpread(elements.page.value);
});
elements.language.addEventListener("change", () => {
  state.language = elements.language.value;
  loadSpread();
});
elements.zoomIn.addEventListener("click", () => setZoom(state.zoom + 0.1));
elements.zoomOut.addEventListener("click", () => setZoom(state.zoom - 0.1));
document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, select, button")) return;
  if (event.key === "ArrowLeft") loadSpread(state.page - 2);
  if (event.key === "ArrowRight") loadSpread(state.page + 2);
});
let resizeFrame = null;
window.addEventListener("resize", () => {
  if (resizeFrame) cancelAnimationFrame(resizeFrame);
  resizeFrame = requestAnimationFrame(() => {
    layoutVisiblePages();
    resizeFrame = null;
  });
});

initialize().catch((error) => setStatus(`初始化失败：${error.message}`, true));
