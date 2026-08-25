/* 登录、注册和图形验证码 */

let authRegisterMode = false;

async function refreshCaptcha() {
  const image = $("#captchaImage");
  image.src = "/api/auth/captcha?ts=" + Date.now();
  $("#authCaptcha").value = "";
}

function showAuthScreen() {
  $(".app").style.visibility = "hidden";
  $("#authScreen").classList.remove("hidden");
  refreshCaptcha();
}

async function initAuth() {
  const state = await fetch("/api/auth/me").then((r) => r.json()).catch(() => ({}));
  if (!state.authenticated) {
    showAuthScreen();
  } else {
    $("#userBadge").textContent = state.username;
  }
  const accountCard = $("#accountCard");
  const toggleAccountCard = (open) => {
    accountCard.classList.toggle("hidden", !open);
    accountCard.setAttribute("aria-hidden", String(!open));
    $("#accountBtn").setAttribute("aria-expanded", String(open));
  };
  $("#accountBtn").onclick = (event) => {
    event.stopPropagation();
    toggleAccountCard(accountCard.classList.contains("hidden"));
  };
  accountCard.onclick = (event) => event.stopPropagation();
  document.addEventListener("click", () => toggleAccountCard(false));
  $("#changePasswordBtn").onclick = () => {
    $("#changePasswordBtn").classList.add("hidden");
    $("#passwordForm").classList.remove("hidden");
    $("#passwordError").textContent = "";
  };
  $("#cancelPasswordBtn").onclick = () => {
    $("#passwordForm").reset();
    $("#passwordForm").classList.add("hidden");
    $("#changePasswordBtn").classList.remove("hidden");
    $("#passwordError").textContent = "";
  };
  $("#passwordForm").onsubmit = async (event) => {
    event.preventDefault();
    const currentPassword = $("#currentPassword").value;
    const newPassword = $("#newPassword").value;
    if (newPassword !== $("#confirmPassword").value) {
      $("#passwordError").textContent = "两次输入的新密码不一致";
      return false;
    }
    const response = await fetch("/api/auth/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      $("#passwordError").textContent = result.error || "修改失败";
      return false;
    }
    $("#passwordForm").reset();
    $("#passwordForm").classList.add("hidden");
    $("#changePasswordBtn").classList.remove("hidden");
    $("#passwordError").textContent = "";
    alert("密码修改成功");
    return false;
  };
  const form = $("#authForm");
  $("#captchaImageBtn").onclick = refreshCaptcha;
  $("#authModeBtn").onclick = () => {
    authRegisterMode = !authRegisterMode;
    $("#authSubtitle").textContent = authRegisterMode ? "创建账号后开始使用" : "登录后继续使用";
    $("#authSubmit").textContent = authRegisterMode ? "注册并登录" : "登录";
    $("#authModeBtn").textContent = authRegisterMode ? "已有账号？登录" : "没有账号？注册";
    $("#authError").textContent = "";
    $("#authPassword").autocomplete = authRegisterMode ? "new-password" : "current-password";
    refreshCaptcha();
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = $("#authSubmit");
    submit.disabled = true;
    $("#authError").textContent = "";
    const endpoint = authRegisterMode ? "/api/auth/register" : "/api/auth/login";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: $("#authUsername").value,
        password: $("#authPassword").value,
        captcha: $("#authCaptcha").value,
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      $("#authError").textContent = result.error || "操作失败，请重试";
      submit.disabled = false;
      refreshCaptcha();
      return false;
    }
    $("#userBadge").textContent = result.username;
    submit.disabled = false;
    window.location.reload();
    return false;
  };
  $("#logoutBtn").onclick = async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.reload();
  };
  return state.authenticated;
}