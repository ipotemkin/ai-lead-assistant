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
const textArea = el('lead-text');
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

/* --- Отправка запроса -------------------------------------------------- */

async function analyze() {
  const text = textArea.value.trim();
  if (!text) {
    showError('Вставьте текст заявки — поле пустое.');
    textArea.focus();
    return;
  }

  clearError();
  resultBox.hidden = true;
  setBusy(true);

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text }),
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

submitBtn.addEventListener('click', analyze);

clearBtn.addEventListener('click', () => {
  textArea.value = '';
  clearError();
  resultBox.hidden = true;
  textArea.focus();
});

textArea.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
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
