(() => {
  const list = document.querySelector("[data-product-list]");
  const cards = list ? [...list.querySelectorAll("[data-product-card]")] : [];
  const search = document.querySelector("[data-filter-search]");
  const storeButtons = [...document.querySelectorAll("[data-filter-store] button")];
  const stockButtons = [...document.querySelectorAll("[data-filter-stock] button")];
  const counter = document.querySelector("[data-filter-count]");
  const empty = document.querySelector("[data-filter-empty]");

  let activeStore = "all";
  let activeStock = "all";

  function normalize(value) {
    return (value || "")
      .toString()
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .trim();
  }

  function setActive(buttons, activeButton) {
    buttons.forEach((button) => {
      button.classList.toggle("active", button === activeButton);
      button.setAttribute("aria-pressed", button === activeButton ? "true" : "false");
    });
  }

  function applyFilters() {
    if (!cards.length) return;

    const term = normalize(search ? search.value : "");
    let visible = 0;

    cards.forEach((card) => {
      const store = normalize(card.dataset.store);
      const stock = normalize(card.dataset.stock);
      const haystack = normalize(card.dataset.search);
      const matchesStore = activeStore === "all" || store === activeStore;
      const matchesStock = activeStock === "all" || stock === activeStock;
      const matchesSearch = !term || haystack.includes(term);
      const shouldShow = matchesStore && matchesStock && matchesSearch;

      card.hidden = !shouldShow;
      if (shouldShow) visible += 1;
    });

    if (counter) {
      counter.textContent = `${visible} de ${cards.length} productos visibles`;
    }
    if (empty) {
      empty.hidden = visible !== 0;
    }
  }

  if (search) {
    search.addEventListener("input", applyFilters);
  }

  storeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeStore = normalize(button.dataset.store || "all");
      setActive(storeButtons, button);
      applyFilters();
    });
  });

  stockButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeStock = normalize(button.dataset.stock || "all");
      setActive(stockButtons, button);
      applyFilters();
    });
  });

  applyFilters();
})();
