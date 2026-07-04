document.addEventListener("DOMContentLoaded", async function () {
    const appEl = document.getElementById('search-results-app');
    if (!appEl) return;
    
    const rawQuery = appEl.dataset.query;
    const category = new URLSearchParams(window.location.search).get('category') || null;
    const resultsContainer = document.getElementById("resultsContainer");
    const resultsLoading = document.getElementById("resultsLoading");
    const resultsEmpty = document.getElementById("resultsEmpty");
    const emptyMessage = document.getElementById("emptyMessage");

    if (!rawQuery) {
        resultsLoading.classList.add("d-none");
        resultsEmpty.classList.remove("d-none");
        emptyMessage.textContent = "검색어가 입력되지 않았습니다.";
        return;
    }

    try {
        const formData = new FormData();
        formData.append("search_text", rawQuery);
        if (category) {
            formData.append("category", category);
        }

        const response = await fetch("/api/search/by-image", {
            method: "POST",
            body: formData,
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "검색에 실패했습니다.");
        }

        resultsLoading.classList.add("d-none");

        if (!data.results || data.results.length === 0) {
            resultsEmpty.classList.remove("d-none");
            return;
        }

        // Populate results
        data.results.forEach((item) => {
            const priceFormatted = item.price.toLocaleString();
            // DB fallback에는 score가 None. score가 있을 때만 유사도 뱃지 표시
            let badgeHtml = "";
            if (item.similarity_score !== null && item.similarity_score !== undefined) {
                // cosine 유사도가 0~1이라고 가정
                let scoreText = (item.similarity_score * 100).toFixed(1) + "% 유사"; 
                if (item.search_source === "elasticsearch_text") {
                    scoreText = "점수: " + item.similarity_score; // Text 검색시엔 백분율 대체
                }
                badgeHtml = `<span class="badge bg-primary position-absolute top-0 end-0 m-2">${scoreText}</span>`;
            }

            const card = document.createElement("div");
            card.className = "col";
            card.innerHTML = `
            <div class="card h-100 product-card position-relative overflow-hidden">
                ${badgeHtml}
                <img src="${item.image_url}" class="card-img-top" alt="${item.product_name}" 
                     onerror="if(this.src.indexOf('${item.local_url}') === -1 && '${item.local_url}' !== 'null') { this.src='${item.local_url}'; } else { this.src='https://placehold.co/400x400?text=No+Image'; }"
                     style="height: 350px; object-fit: cover; object-position: top;">
                <div class="card-body">
                    <small class="text-muted d-block mb-1">${item.brand}</small>
                    <h6 class="card-title text-truncate fw-bold">${item.product_name}</h6>
                    <div class="d-flex align-items-center mt-2">
                        <span class="text-danger fw-bold fs-5 me-2">${priceFormatted}원</span>
                    </div>
                    <div class="text-muted small mt-1">${item.mall_name}</div>
                </div>
                <div class="card-footer bg-white border-top-0 pt-0">
                    <a href="/product/${item.product_id}" class="btn btn-dark w-100 btn-sm">상품 상세보기 <i class="fas fa-external-link-alt ms-1"></i></a>
                </div>
            </div>
        `;
            resultsContainer.appendChild(card);
        });

    } catch (error) {
        console.error("검색 오류:", error);
        resultsLoading.classList.add("d-none");
        resultsEmpty.classList.remove("d-none");
        emptyMessage.textContent = error.message || "서버 통신 중 오류가 발생했습니다.";
    }
});
