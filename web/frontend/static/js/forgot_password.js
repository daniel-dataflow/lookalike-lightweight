let generatedTempPassword = "";

document.addEventListener('DOMContentLoaded', function() {
  const form = document.getElementById('forgotPasswordForm');
  if (!form) return;

  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    
    // 에러 초기화
    hideError('forgotEmailError');
    hideError('forgotQuestionError');
    hideError('forgotAnswerError');
    hideError('forgotGeneralError');

    const email = document.getElementById('forgotEmail').value.trim();
    const question = document.getElementById('forgotSecurityQuestion').value;
    const answer = document.getElementById('forgotSecurityAnswer').value.trim();

    // 유효성 체크
    if (!email) return showError('forgotEmailError', '이메일 주소를 입력해주세요.');
    if (!question) return showError('forgotQuestionError', '보안 질문을 선택해주세요.');
    if (!answer) return showError('forgotAnswerError', '보안 질문 답변을 입력해주세요.');

    setLoading(true);

    try {
      const res = await fetch('/api/auth/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, security_question: question, security_answer: answer })
      });
      const data = await res.json();

      if (res.ok && data.success) {
        generatedTempPassword = data.temp_password;
        document.getElementById('tempPasswordPlaceholder').textContent = generatedTempPassword;
        
        // 폼 영역 숨기고 결과 영역 노출
        document.getElementById('forgotFormArea').classList.add('d-none');
        document.getElementById('forgotResultArea').classList.remove('d-none');
      } else {
        showError('forgotGeneralError', data.detail || '인증 정보가 일치하지 않거나 오류가 발생했습니다.');
      }
    } catch (err) {
      showError('forgotGeneralError', '서버 통신 실패: ' + err.message);
    } finally {
      setLoading(false);
    }
  });
});

function showError(elementId, message) {
  const el = document.getElementById(elementId);
  if (el) {
    el.textContent = message;
    el.classList.remove('d-none');
  }
}

function hideError(elementId) {
  const el = document.getElementById(elementId);
  if (el) {
    el.textContent = '';
    el.classList.add('d-none');
  }
}

function setLoading(isLoading) {
  const btn = document.getElementById('forgotSubmitBtn');
  const spinner = document.getElementById('forgotSpinner');
  if (btn) btn.disabled = isLoading;
  if (spinner) spinner.classList.toggle('d-none', !isLoading);
}

function copyTempPassword() {
  if (!generatedTempPassword) return;
  navigator.clipboard.writeText(generatedTempPassword).then(() => {
    alert('임시 비밀번호가 클립보드에 복사되었습니다.');
  }).catch(() => {
    alert('비밀번호 복사에 실패했습니다. 수동으로 복사해 주세요.');
  });
}
