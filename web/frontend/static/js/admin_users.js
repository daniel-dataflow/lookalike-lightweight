let activeUsersSubTab = 'admin'; // 'admin', 'general', 'non-login'

document.addEventListener('DOMContentLoaded', function() {
  loadUsersData();
});

function switchUsersTab(tabName) {
  activeUsersSubTab = tabName;
  loadUsersData();
}

async function loadUsersData() {
  const containerId = activeUsersSubTab === 'admin' ? 'adminUsersTableBody' : 
                      (activeUsersSubTab === 'general' ? 'generalUsersTableBody' : 'nonloginUsersTableBody');
  const container = document.getElementById(containerId);
  if (!container) return;
  
  container.innerHTML = '<tr><td colspan="10" class="text-center py-4 text-muted"><i class="fas fa-spinner fa-spin me-1"></i>데이터를 불러오는 중입니다...</td></tr>';
  
  try {
    const res = await fetch(`/api/admin/users/list?tab=${activeUsersSubTab}`);
    const data = await res.json();
    
    if (res.ok && data.success) {
      if (activeUsersSubTab === 'admin') {
        renderAdminUsers(data.users);
      } else if (activeUsersSubTab === 'general') {
        renderGeneralUsers(data.users);
      } else {
        renderNonLoginUsers(data.users);
      }
    } else {
      container.innerHTML = `<tr><td colspan="10" class="text-center py-4 text-danger"><i class="fas fa-exclamation-circle me-1"></i>데이터 로드 실패: ${data.detail || '서버 오류'}</td></tr>`;
    }
  } catch (err) {
    container.innerHTML = `<tr><td colspan="10" class="text-center py-4 text-danger"><i class="fas fa-exclamation-circle me-1"></i>네트워크 오류: ${err.message}</td></tr>`;
  }
}

function parsePermissionBadges(perm) {
  if (!perm) return '<span class="badge bg-light text-muted">권한 없음</span>';
  if (perm === 'SUPER_ADMIN') {
    return '<span class="badge bg-danger text-white"><i class="fas fa-crown me-1"></i>SUPER_ADMIN</span>';
  }
  
  const mapping = {
    'infra': '인프라',
    'crawling': '크롤링',
    'logs': '로그',
    'visitors': '방문자',
    'inquiry': '문의'
  };
  
  return perm.split(',').map(p => {
    const val = p.trim();
    if (!val) return '';
    const label = mapping[val] || val;
    return `<span class="badge bg-light text-dark border me-1">${label}</span>`;
  }).join('');
}

function renderAdminUsers(users) {
  const tbody = document.getElementById('adminUsersTableBody');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted">등록된 관리자가 없습니다.</td></tr>';
    return;
  }
  
  tbody.innerHTML = users.map(u => `
    <tr>
      <td class="fw-bold text-dark" style="padding-left: 20px;">${u.user_id}</td>
      <td>${u.name}</td>
      <td>${u.email}</td>
      <td>
        ${parsePermissionBadges(u.admin_permission)}
      </td>
      <td>${u.create_dt ? new Date(u.create_dt).toLocaleString('ko-KR') : '—'}</td>
      <td class="text-center">
        <button class="btn btn-xs btn-outline-primary me-1" onclick="openEditPermissionModal('${u.user_id}', '${u.admin_permission || ''}')">
          <i class="fas fa-edit me-1"></i>권한수정
        </button>
        <button class="btn btn-xs btn-outline-danger" onclick="openResetAdminPasswordModal('${u.user_id}')">
          <i class="fas fa-key me-1"></i>비번재설정
        </button>
      </td>
    </tr>
  `).join('');
}

function renderGeneralUsers(users) {
  const tbody = document.getElementById('generalUsersTableBody');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center py-4 text-muted">가입된 회원이 없습니다.</td></tr>';
    return;
  }
  
  tbody.innerHTML = users.map(u => `
    <tr>
      <td class="fw-bold text-dark" style="padding-left: 20px;">${u.user_id}</td>
      <td>${u.name || '—'}</td>
      <td>${u.email || '—'}</td>
      <td>
        <span class="badge ${u.provider === 'kakao' ? 'bg-warning text-dark' : (u.provider === 'naver' ? 'bg-success text-white' : 'bg-secondary text-white')}" style="font-size:0.7rem;">
          ${u.provider}
        </span>
      </td>
      <td>${u.create_dt ? new Date(u.create_dt).toLocaleDateString('ko-KR') : '—'}</td>
      <td class="text-center fw-bold text-primary">${u.total_search_count || 0}회</td>
      <td class="text-muted" style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
        ${u.recent_keywords ? u.recent_keywords.split(', ').map(k => `<span class="badge bg-light text-dark border me-1">${k}</span>`).join('') : '—'}
      </td>
      <td>${u.last_search_dt ? new Date(u.last_search_dt).toLocaleString('ko-KR') : '—'}</td>
      <td class="text-center">
        ${u.provider === 'email' ? `
          <button class="btn btn-xs btn-outline-warning" onclick="resetUserPassword('${u.user_id}')">
            <i class="fas fa-undo me-1"></i>초기화
          </button>
        ` : `<span class="text-muted small">소셜 로그인</span>`}
      </td>
    </tr>
  `).join('');
}

function renderNonLoginUsers(users) {
  const tbody = document.getElementById('nonloginUsersTableBody');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-center py-4 text-muted">비로그인 활동 로그가 없습니다.</td></tr>';
    return;
  }
  
  tbody.innerHTML = users.map(u => `
    <tr>
      <td class="fw-bold text-info" style="padding-left: 20px;">${u.ip_address}</td>
      <td class="text-muted small" style="max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${u.user_agent || ''}">
        ${u.user_agent || '—'}
      </td>
      <td class="text-center fw-bold text-secondary">${u.total_search_count || 0}회</td>
      <td>
        ${u.recent_keywords ? u.recent_keywords.split(', ').map(k => `<span class="badge bg-light text-dark border me-1">${k}</span>`).join('') : '—'}
      </td>
      <td>${u.last_search_dt ? new Date(u.last_search_dt).toLocaleString('ko-KR') : '—'}</td>
    </tr>
  `).join('');
}

function toggleSuperAdminCheck(chk) {
  const perms = document.querySelectorAll('.new-admin-perm');
  perms.forEach(p => {
    p.disabled = chk.checked;
    if (chk.checked) p.checked = true;
  });
}

function toggleEditSuperAdminCheck(chk) {
  const perms = document.querySelectorAll('.edit-admin-perm');
  perms.forEach(p => {
    p.disabled = chk.checked;
    if (chk.checked) p.checked = true;
  });
}

async function submitCreateAdmin() {
  const username = document.getElementById('newAdminUsername').value.trim();
  const name = document.getElementById('newAdminName').value.trim();
  const email = document.getElementById('newAdminEmail').value.trim();
  const password = document.getElementById('newAdminPassword').value;
  
  if (!username || !name || !email || !password) {
    alert('모든 필드를 채워주세요.');
    return;
  }
  
  let permission = '';
  const isSuper = document.getElementById('permSuperAdmin').checked;
  if (isSuper) {
    permission = 'SUPER_ADMIN';
  } else {
    const selected = [];
    document.querySelectorAll('.new-admin-perm:checked').forEach(p => {
      selected.push(p.value);
    });
    permission = selected.join(',');
  }
  
  try {
    const res = await fetch('/api/admin/users/create-admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, name, email, password, permission })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '신규 어드민 등록 완료!');
      document.getElementById('createAdminForm').reset();
      
      // 비활성화 체크박스 복구
      document.querySelectorAll('.new-admin-perm').forEach(p => p.disabled = false);
      
      // 모달 닫기
      const modalEl = document.getElementById('createAdminModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) modal.hide();
      
      loadUsersData();
    } else {
      alert(`등록 실패: ${data.detail || '서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 오류: ${err.message}`);
  }
}

function openEditPermissionModal(username, permission) {
  document.getElementById('editPermissionTarget').value = username;
  document.getElementById('editPermissionTargetLabel').textContent = `대상 관리자: ${username}`;
  
  const isSuper = permission === 'SUPER_ADMIN';
  const superChk = document.getElementById('editPermSuperAdmin');
  superChk.checked = isSuper;
  
  const perms = document.querySelectorAll('.edit-admin-perm');
  const allowedList = permission.split(',').map(p => p.trim());
  
  perms.forEach(p => {
    p.disabled = isSuper;
    p.checked = isSuper || allowedList.includes(p.value);
  });
  
  const modal = new bootstrap.Modal(document.getElementById('editAdminPermissionModal'));
  modal.show();
}

async function submitUpdatePermission() {
  const username = document.getElementById('editPermissionTarget').value;
  const isSuper = document.getElementById('editPermSuperAdmin').checked;
  
  let permission = '';
  if (isSuper) {
    permission = 'SUPER_ADMIN';
  } else {
    const selected = [];
    document.querySelectorAll('.edit-admin-perm:checked').forEach(p => {
      selected.push(p.value);
    });
    permission = selected.join(',');
  }
  
  try {
    const res = await fetch('/api/admin/users/update-permission', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, permission })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '권한 정보가 성공적으로 업데이트되었습니다.');
      
      const modalEl = document.getElementById('editAdminPermissionModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) modal.hide();
      
      loadUsersData();
    } else {
      alert(`업데이트 실패: ${data.detail || '서버 권한 제한'}`);
    }
  } catch (err) {
    alert(`네트워크 오류: ${err.message}`);
  }
}

function openResetAdminPasswordModal(username) {
  document.getElementById('resetAdminPasswordTarget').value = username;
  document.getElementById('resetPasswordTargetLabel').textContent = `대상 관리자: ${username}`;
  document.getElementById('resetAdminNewPassword').value = '';
  
  const modal = new bootstrap.Modal(document.getElementById('resetAdminPasswordModal'));
  modal.show();
}

async function submitResetAdminPassword() {
  const username = document.getElementById('resetAdminPasswordTarget').value;
  const new_password = document.getElementById('resetAdminNewPassword').value;
  
  if (!new_password || new_password.length < 4) {
    alert('비밀번호는 최소 4자 이상이어야 합니다.');
    return;
  }
  
  try {
    const res = await fetch('/api/admin/users/reset-admin-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, new_password })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || '비밀번호 강제 변경 성공!');
      
      const modalEl = document.getElementById('resetAdminPasswordModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) modal.hide();
    } else {
      alert(`재설정 실패: ${data.detail || '서버 권한 제한'}`);
    }
  } catch (err) {
    alert(`네트워크 오류: ${err.message}`);
  }
}

async function resetUserPassword(userId) {
  if (!confirm(`'${userId}' 사용자의 패스워드를 1회용 임시 비밀번호로 즉시 초기화하시겠습니까?`)) {
    return;
  }
  
  try {
    const res = await fetch('/api/admin/users/reset-user-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(`[비밀번호 초기화 성공]\n\n해당 사용자의 임시 비밀번호가 발급되었습니다.\n아래 비밀번호를 복사하여 회원에게 직접 안전하게 전달해 주세요.\n\n임시 비밀번호: ${data.temp_password}`);
      loadUsersData();
    } else {
      alert(`초기화 실패: ${data.detail || '서버 오류'}`);
    }
  } catch (err) {
    alert(`네트워크 오류: ${err.message}`);
  }
}
