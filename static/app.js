(() => {
  const list = document.querySelector("[data-product-list]");
  if (!list) return;

  const cards = [...list.querySelectorAll("[data-product-card]")];
  const search = document.querySelector("[data-filter-search]");
  const storeButtons = [...document.querySelectorAll("[data-filter-store] button")];
  let activeStore = "all";

  function normalize(value) {
    return (value || "")
      .toString()
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  function applyFilters() {
    const term = normalize(search ? search.value : "");
    cards.forEach((card) => {
      const matchesStore = activeStore === "all" || card.dataset.store === activeStore;
      const matchesSearch = normalize(card.dataset.search).includes(term);
      card.hidden = !(matchesStore && matchesSearch);
    });
  }

  if (search) {
    search.addEventListener("input", applyFilters);
  }

  storeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeStore = button.dataset.store || "all";
      storeButtons.forEach((item) => item.classList.toggle("active", item === button));
      applyFilters();
    });
  });
})();
