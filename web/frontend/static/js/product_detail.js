document.addEventListener('DOMContentLoaded', async function () {
    const appEl = document.getElementById('product-detail-app');
    if (!appEl) return;
    
    const productId = appEl.dataset.productId;
    const likeBtn = document.getElementById('likeBtn');
    const likeIcon = document.getElementById('likeIcon');
    let isLiked = false;

    // 1. 페이지 로드 시 자동으로 조회 기록
    try {
        await fetch(`/api/products/${productId}/view`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include'
        });
    } catch (error) {
        console.error('조회 기록 실패:', error);
    }

    // 2. 좋아요 상태 확인
    try {
        const response = await fetch(`/api/products/${productId}/like-status`, { credentials: 'include' });
        const data = await response.json();
        if (data.success && data.liked) {
            isLiked = true;
            likeIcon.classList.remove('far');
            likeIcon.classList.add('fas');
        }
    } catch (error) {
        console.error('좋아요 상태 확인 실패:', error);
    }

    // 3. 좋아요 버튼 클릭 이벤트
    likeBtn.addEventListener('click', async function () {
        try {
            if (isLiked) {
                // 좋아요 취소
                const response = await fetch(`/api/products/${productId}/like`, {
                    method: 'DELETE',
                    credentials: 'include'
                });
                const data = await response.json();
                if (data.success) {
                    isLiked = false;
                    likeIcon.classList.remove('fas');
                    likeIcon.classList.add('far');
                } else if (data.message) {
                    alert(data.message);
                }
            } else {
                // 좋아요 추가
                const response = await fetch(`/api/products/${productId}/like`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include'
                });
                const data = await response.json();
                if (data.success) {
                    isLiked = true;
                    likeIcon.classList.remove('far');
                    likeIcon.classList.add('fas');
                } else if (data.message) {
                    alert(data.message);
                }
            }
        } catch (error) {
            console.error('좋아요 처리 실패:', error);
            alert('로그인이 필요합니다.');
        }
    });
});
