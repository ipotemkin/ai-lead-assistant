/* Логика страницы: отправка заявки и отрисовка карточки. */

const KEY_LABELS = {
  company: 'Компания',
  name: 'Имя',
  contact: 'Контакт',
  product: 'Продукт',
  quantity: 'Количество',
  budget: 'Бюджет',
  deadline: 'Сроки',
};

const el = (id) => document.getElementById(id);

const submitBtn = el('submit-btn');
const clearBtn = el('clear-btn');
const form = el('lead-form');
const nameInput = el('f-name');
const phoneInput = el('f-phone');
const emailInput = el('f-email');
const requestInput = el('f-request');
const errRequest = el('err-request');
const errPhone = el('err-phone');
const errEmail = el('err-email');
const previewBody = el('llm-preview-body');
const previewMeta = el('llm-preview-meta');
const errorBox = el('error-box');
const resultBox = el('result');
const spinner = el('spinner');
const statusText = el('status-text');

let lastCard = null;

/* --- Вспомогательные функции ------------------------------------------- */

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  resultBox.hidden = true;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = '';
}

function setBusy(isBusy) {
  submitBtn.disabled = isBusy;
  submitBtn.textContent = isBusy ? 'Обрабатываю…' : 'Обработать';
  spinner.hidden = !isBusy;
  statusText.hidden = !isBusy;
  if (isBusy) statusText.textContent = 'Анализирую заявку, обычно это занимает 10–25 секунд…';
}

function fillChip(container, items, emptyText) {
  container.textContent = '';
  if (!items || items.length === 0) {
    const span = document.createElement('span');
    span.className = 'chip empty';
    span.textContent = emptyText;
    container.appendChild(span);
    return;
  }
  items.forEach((item) => {
    const span = document.createElement('span');
    span.className = 'chip';
    span.textContent = item;
    container.appendChild(span);
  });
}

/* --- Отрисовка карточки ----------------------------------------------- */

function renderCard(card) {
  el('f-company').textContent = card.company || 'не указано';
  el('f-summary').textContent = card.summary || 'не указано';
  el('f-lead-type-reason').textContent = card.lead_type_reason || 'не указано';
  el('f-next-step').textContent = card.next_step || 'не указано';
  el('f-human-reason').textContent = card.human_reason || 'не требуется';

  const reply = el('f-reply');
  reply.textContent = card.reply_draft || 'не указано';

  // Бейдж типа лида
  const badge = el('lead-badge');
  badge.className = 'pill';
  if (card.lead_type === 'целевой') {
    badge.classList.add('pill-green');
  } else if (card.lead_type === 'нецелевой') {
    badge.classList.add('pill-red');
  } else {
    badge.classList.add('pill-amber');
  }
  badge.textContent = `Тип лида: ${card.lead_type}`;

  // Бейдж «нужен человек»
  const humanBadge = el('human-badge');
  if (card.needs_human) {
    humanBadge.className = 'pill pill-amber';
    humanBadge.textContent = 'Нужен человек';
    humanBadge.hidden = false;
  } else {
    humanBadge.hidden = true;
  }

  // Известные данные
  const keyData = el('f-key-data');
  keyData.textContent = '';
  Object.entries(KEY_LABELS).forEach(([key, label]) => {
    const value = (card.key_data && card.key_data[key]) || 'не указано';
    const box = document.createElement('div');
    box.className = 'kd';

    const labelEl = document.createElement('span');
    labelEl.className = 'kd-label';
    labelEl.textContent = label;

    const valueEl = document.createElement('span');
    valueEl.className = value === 'не указано' ? 'kd-value empty' : 'kd-value';
    valueEl.textContent = value;

    box.appendChild(labelEl);
    box.appendChild(valueEl);
    keyData.appendChild(box);
  });

  fillChip(el('f-missing'), card.missing_data, 'Всё есть — ничего не пропущено');
  fillChip(el('f-questions'), card.clarifying_questions, 'Вопросы не нужны');

  resultBox.hidden = false;
}

function cardToText(card) {
  const lines = [
    `Тип лида: ${card.lead_type}`,
    `Нужен человек: ${card.needs_human ? 'да' : 'нет'}`,
    `Причина: ${card.human_reason || 'не требуется'}`,
    '',
    `Компания: ${card.company}`,
    `Суть запроса: ${card.summary}`,
    `Почему такой тип лида: ${card.lead_type_reason || 'не указано'}`,
    '',
    'Известные данные:',
  ];
  Object.entries(KEY_LABELS).forEach(([key, label]) => {
    const value = (card.key_data && card.key_data[key]) || 'не указано';
    lines.push(`  ${label}: ${value}`);
  });
  lines.push('');
  lines.push(`Следующий шаг: ${card.next_step}`);
  lines.push(
    `Чего не хватает: ${(card.missing_data && card.missing_data.length) ? card.missing_data.join(', ') : 'ничего'}`,
  );
  if (card.clarifying_questions && card.clarifying_questions.length) {
    lines.push('Уточняющие вопросы:');
    card.clarifying_questions.forEach((q) => lines.push(`  - ${q}`));
  }
  lines.push('');
  lines.push('Черновик ответа:');
  lines.push(card.reply_draft || 'не указано');
  return lines.join('\n');
}

async function copyText(text, button) {
  const original = button.textContent;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      document.body.removeChild(area);
    }
    button.textContent = 'Скопировано';
  } catch (err) {
    button.textContent = 'Не удалось скопировать';
  }
  setTimeout(() => { button.textContent = original; }, 1800);
}

/* --- Предпросмотр: что уйдёт в модель ---------------------------------- */

let previewTimer = null;
let previewSeq = 0;

function formPayload() {
  return {
    text: requestInput.value.trim(),
    name: nameInput.value.trim(),
    phone: phoneInput.value.trim(),
    email: emailInput.value.trim(),
  };
}

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 400);
}

async function updatePreview() {
  const payload = formPayload();
  const isEmpty = !payload.text && !payload.name && !payload.phone && !payload.email;

  if (isEmpty) {
    previewBody.textContent = 'Заполните форму — здесь появится текст, который увидит модель.';
    previewMeta.textContent = '';
    return;
  }

  const seq = ++previewSeq;
  try {
    const response = await fetch('/api/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) return;
    const data = await response.json();
    if (seq !== previewSeq) return; // пока пришёл более свежий ответ
    previewBody.textContent = data.llm_request || '(пусто)';
    previewMeta.textContent = data.summary ? `скрыто: ${data.summary}` : 'персональных данных нет';
  } catch (err) {
    /* предпросмотр не критичен — молча оставляем прошлый текст */
  }
}

/* --- Проверка полей --------------------------------------------------- */

function showFieldError(input, box, message) {
  if (box) {
    box.textContent = message;
    box.hidden = false;
  }
  if (input) input.classList.add('invalid');
}

function clearFieldError(input, box) {
  if (box) {
    box.textContent = '';
    box.hidden = true;
  }
  if (input) input.classList.remove('invalid');
}

function validate() {
  clearFieldError(requestInput, errRequest);
  clearFieldError(emailInput, errEmail);
  clearFieldError(phoneInput, errPhone);

  let ok = true;

  const request = requestInput.value.trim();
  if (!request) {
    showFieldError(requestInput, errRequest, 'Впишите текст заявки — без него обрабатывать нечего.');
    ok = false;
  }

  const email = emailInput.value.trim();
  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    showFieldError(emailInput, errEmail, 'Проверьте адрес почты — похоже, в нём опечатка.');
    ok = false;
  }

  const phone = phoneInput.value.trim();
  if (phone && phone.replace(/\D/g, '').length < 6) {
    showFieldError(phoneInput, errPhone, 'Проверьте телефон — слишком мало цифр.');
    ok = false;
  }

  return ok;
}

/* --- Отправка запроса -------------------------------------------------- */

async function analyze() {
  if (!validate()) {
    errorBox.hidden = true;
    return;
  }

  clearError();
  resultBox.hidden = true;
  setBusy(true);

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formPayload()),
    });

    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }

    if (!response.ok) {
      const message = (data && data.error) || `Ошибка сервера (HTTP ${response.status}).`;
      showError(message);
      return;
    }

    lastCard = data;
    renderCard(data);

    // Показываем фактически отправленный текст (он обезличен)
    if (data.llm_request) {
      previewBody.textContent = data.llm_request;
      const counts = {};
      (data.entities || []).forEach((item) => {
        counts[item.type] = (counts[item.type] || 0) + 1;
      });
      const summary = Object.keys(counts)
        .map((name) => `${name} (${counts[name]})`)
        .join(', ');
      previewMeta.textContent = summary ? `скрыто: ${summary}` : 'персональных данных нет';
    }

    resultBox.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    showError('Нет связи с приложением. Убедитесь, что сервер запущен, и попробуйте ещё раз.');
  } finally {
    setBusy(false);
  }
}

/* --- Инициализация ----------------------------------------------------- */

async function loadHealth() {
  try {
    const response = await fetch('/health');
    if (!response.ok) return;
    const health = await response.json();

    const pill = el('model-pill');
    pill.textContent = health.model;
    pill.hidden = false;

    if (!health.api_key_present) {
      const alert = el('health-alert');
      alert.textContent =
        'Не найден ключ API. Откройте файл .env рядом с app.py, впишите ключ в строку PROXY_API_KEY и перезапустите приложение.';
      alert.hidden = false;
    } else if (!health.company_description_present) {
      const alert = el('health-alert');
      alert.textContent =
        'Описание продуктов компании не задано. Заполните COMPANY_DESCRIPTION в .env — без него тип лида определяется хуже.';
      alert.hidden = false;
    }
  } catch (err) {
    /* сервер недоступен — просто не показываем подсказку */
  }
}

// Отправка по кнопке и по Enter в любом поле формы
form.addEventListener('submit', (event) => {
  event.preventDefault();
  analyze();
});

clearBtn.addEventListener('click', () => {
  form.reset();
  clearFieldError(requestInput, errRequest);
  clearFieldError(phoneInput, errPhone);
  clearFieldError(emailInput, errEmail);
  clearError();
  resultBox.hidden = true;
  previewBody.textContent = 'Заполните форму — здесь появится текст, который увидит модель.';
  previewMeta.textContent = '';
  requestInput.focus();
});

[requestInput, emailInput].forEach((input) => {
  input.addEventListener('input', () => {
    if (input === emailInput) clearFieldError(emailInput, errEmail);
    if (input === requestInput) clearFieldError(requestInput, errRequest);
    schedulePreview();
  });
});

phoneInput.addEventListener('input', () => {
  clearFieldError(phoneInput, errPhone);
  schedulePreview();
});

nameInput.addEventListener('input', schedulePreview);

// Ctrl + Enter (Cmd + Enter на Mac) отправляет заявку из многострочного поля
requestInput.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    analyze();
  }
});

el('copy-card').addEventListener('click', (event) => {
  if (lastCard) copyText(cardToText(lastCard), event.currentTarget);
});

el('copy-reply').addEventListener('click', (event) => {
  if (lastCard) copyText(lastCard.reply_draft || 'не указано', event.currentTarget);
});

loadHealth();
