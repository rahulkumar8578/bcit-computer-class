
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const panelIds = ['home', 'about', 'courses', 'admission', 'profile', 'enquiry', 'assistant'];

function closeMobileMenu() {
    const nav = $('#nav');
    const toggle = $('#menuToggle');
    nav.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
}

function showTab(id) {
    if (!panelIds.includes(id)) id = 'home';

    $$('.tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === id);
    });

    $$('#nav a').forEach(link => {
        const active = link.getAttribute('href') === '#' + id;
        link.classList.toggle('active', active);

        if (active) {
            link.setAttribute('aria-current', 'page');
        } else {
            link.removeAttribute('aria-current');
        }
    });

    closeMobileMenu();
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

window.addEventListener('hashchange', () => {
    showTab(location.hash.slice(1));
});

$$('#nav a').forEach(a => {
    a.addEventListener('click', e => {
        const href = a.getAttribute('href') || '';

        if (!href.startsWith('#')) {
            closeMobileMenu();
            return;
        }

        e.preventDefault();

        const id = href.slice(1);

        if (location.hash === '#' + id) {
            showTab(id);
        } else {
            location.hash = id;
        }
    });
});

$$('.hero a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
        e.preventDefault();
        location.hash = a.getAttribute('href').slice(1);
    });
});

showTab(location.hash.slice(1) || 'home');

$('#year').textContent = new Date().getFullYear();

$('#menuToggle').setAttribute('aria-expanded', 'false');
$('#menuToggle').setAttribute('aria-controls', 'nav');
$('#menuToggle').addEventListener('click', () => {
    const isOpen = $('#nav').classList.toggle('open');
    $('#menuToggle').setAttribute('aria-expanded', String(isOpen));
});

document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeMobileMenu();
});

document.addEventListener('click', event => {
    if (!event.target.closest('.topbar')) closeMobileMenu();
});
// Top-right notification system
(function () {
    const style = document.createElement('style');

    style.textContent = `
        #bcitToastContainer {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 999999;
            display: flex;
            flex-direction: column;
            gap: 10px;
            max-width: calc(100vw - 40px);
        }

        .bcit-toast {
            min-width: 260px;
            max-width: 360px;
            padding: 14px 18px;
            border-radius: 8px;
            color: #fff;
            font-size: 14px;
            font-weight: 600;
            font-family: Arial, sans-serif;
            box-shadow: 0 5px 20px rgba(0,0,0,.25);
            animation: bcitToastIn .3s ease;
        }

        .bcit-toast.success {
            background: #16803c;
        }

        .bcit-toast.error {
            background: #c62828;
        }

        .bcit-toast.hide {
            opacity: 0;
            transform: translateX(30px);
            transition: .3s ease;
        }

        @keyframes bcitToastIn {
            from {
                opacity: 0;
                transform: translateX(30px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }

        @media (max-width: 480px) {
            #bcitToastContainer {
                top: 12px;
                right: 12px;
                left: 12px;
            }

            .bcit-toast {
                min-width: 0;
                max-width: 100%;
            }
        }
    `;

    document.head.appendChild(style);

    const container = document.createElement('div');
    container.id = 'bcitToastContainer';
    document.body.appendChild(container);

    window.showBCITToast = function (message, type = 'success') {
        const toast = document.createElement('div');
        let safeMessage = String(message || '');
        if (type === 'error' && /https?:\/\/|\/api\/|traceback|sqlite|sqlalchemy|typeerror|referenceerror|stack/i.test(safeMessage)) {
            safeMessage = 'The action could not be completed. Please try again.';
        }

        toast.className = 'bcit-toast ' + type;
        toast.textContent = safeMessage;

        container.appendChild(toast);

        setTimeout(() => {
            toast.classList.add('hide');

            setTimeout(() => toast.remove(), 300);
        }, 4000);
    };
})();

/* BCIT unified operation feedback: success/error, duplicate-submit guard and refresh signal. */
(function () {
    if (window.__bcitOperationFeedbackInstalled) return;
    window.__bcitOperationFeedbackInstalled = true;
    let noticeVersion = Number(window.__bcitManualNoticeVersion || 0);
    let lastSubmitter = null;
    const originalFetch = window.fetch.bind(window);
    function currentNoticeVersion() { return Math.max(noticeVersion, Number(window.__bcitManualNoticeVersion || 0)); }

    function notify(message, type) {
        if (typeof window.showBCITToast === 'function') window.showBCITToast(message, type);
        else if (typeof window.showToast === 'function') window.showToast(message, type);
        else {
            const el = document.getElementById('bcitToast');
            if (el) {
                el.textContent = message;
                el.classList.toggle('error', type === 'error');
                el.classList.add('show');
                clearTimeout(window.__bcitGenericToastTimer);
                window.__bcitGenericToastTimer = setTimeout(() => el.classList.remove('show'), 4000);
            }
        }
    }

    ['showBCITToast', 'showToast'].forEach(name => {
        if (typeof window[name] !== 'function') return;
        const original = window[name];
        window[name] = function () {
            noticeVersion++;
            window.__bcitManualNoticeVersion = noticeVersion;
            return original.apply(this, arguments);
        };
    });

    document.addEventListener('click', function (event) {
        const button = event.target && event.target.closest
            ? event.target.closest('button, input[type="submit"], input[type="button"]') : null;
        if (button && !button.disabled) lastSubmitter = button;
    }, true);
    document.addEventListener('submit', function (event) {
        if (event.submitter) lastSubmitter = event.submitter;
    }, true);

    window.fetch = async function (input, init) {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        const mutating = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method);
        const excluded = /\/api\/student\/profile(?:\?|$)/i.test(url) || /\/api\/(?:assistant|ai)(?:\/|$)/i.test(url) || /\/api\/(?:student\/)?ai-assistant(?:\/|\?|$)/i.test(url) ||
            /\/api\/student\/online-exams\/[^/]+\/(answers|violation)(\/|$)/i.test(url) ||
            /\/api\/student\/login(?:\?|$)/i.test(url);
        if (!mutating || excluded) {
            if (method === 'GET' && /\/api\//i.test(url) && !(init && init.cache)) {
                return originalFetch(input, Object.assign({}, init || {}, { cache: 'no-store' }));
            }
            return originalFetch(input, init);
        }

        const before = currentNoticeVersion();
        const button = lastSubmitter && document.contains(lastSubmitter) ? lastSubmitter :
            (document.activeElement && document.activeElement.matches &&
             document.activeElement.matches('button, input[type="submit"]') ? document.activeElement : null);
        const wasDisabled = !!(button && button.disabled);
        if (button && !wasDisabled) {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        }

        try {
            const response = await originalFetch(input, init);
            if (response.ok) {
                window.dispatchEvent(new CustomEvent('bcit:data-changed', { detail: { method: method } }));
                setTimeout(() => {
                    if (currentNoticeVersion() === before) notify('Changes saved successfully.', 'success');
                }, 120);
            } else {
                setTimeout(() => {
                    if (currentNoticeVersion() === before) notify('The action could not be completed. Please try again.', 'error');
                }, 120);
            }
            return response;
        } catch (error) {
            setTimeout(() => {
                if (currentNoticeVersion() === before) notify('The action could not be completed. Please try again.', 'error');
            }, 120);
            throw error;
        } finally {
            if (button && !wasDisabled) {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            }
            if (lastSubmitter === button) lastSubmitter = null;
        }
    };

})();

if (new URLSearchParams(location.search).get('student_login') === 'success') {
    showBCITToast('Login successful.', 'success');
    history.replaceState(null, '', location.pathname + location.hash);
}

function status(el, msg, ok) {
    el.textContent = msg;
    el.className = 'form-status ' + (ok ? 'success' : 'error');
}

// Admission date defaults to today's local date.
const admissionDateField = $('#admissionForm').elements.admissionDate;

if (admissionDateField && !admissionDateField.value) {
    const now = new Date();

    admissionDateField.value =
        `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
}

// Payment QR
const paymentQr = $('#paymentQr');
const paymentQrNotice = $('#paymentQrNotice');

if (paymentQr) {
    paymentQr.addEventListener('load', () => {
        if (paymentQrNotice) {
            paymentQrNotice.textContent = 'Scan this QR in a UPI app and pay exactly ₹500.';
        }
    });

    paymentQr.addEventListener('error', async () => {
        paymentQr.style.display = 'none';

        let message = 'The institute QR code could not be loaded. Restart the server and refresh the page.';

        try {
            const response = await fetch('/api/payment/qr', {
                cache: 'no-store'
            });

            const result = await response.json();

            if (result && result.error) {
                message = result.error;
            } else if (!response.ok) {
                message = `QR request failed (HTTP ${response.status}).`;
            }
        } catch (_) {
            message = 'Unable to connect to the QR server. Run the website using python app.py; do not open index.html directly.';
        }

        if (paymentQrNotice) {
            paymentQrNotice.textContent = message;
        }
    });
}

// Admission form
const admissionForm = $('#admissionForm');
const admissionPayment = $('#admissionPayment');

const paymentReferenceField = admissionForm.elements.paymentReference;
const paymentDateTimeField = admissionForm.elements.paymentDateTime;

if (paymentDateTimeField) {
    const d = new Date();

    paymentDateTimeField.value =
        `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

const saveNextBtn = $('#saveNextBtn');
const submitAdmissionBtn = $('#submitAdmissionBtn');
const backToDetailsBtn = $('#backToDetailsBtn');

function showPaymentStep() {
    admissionPayment.hidden = false;
    paymentReferenceField.disabled = false;

    if (paymentDateTimeField) {
        paymentDateTimeField.disabled = false;
    }

    saveNextBtn.hidden = true;
    submitAdmissionBtn.hidden = false;

    admissionPayment.scrollIntoView({
        behavior: 'smooth',
        block: 'start'
    });

    paymentReferenceField.focus({
        preventScroll: true
    });
}

function showDetailsStep() {
    admissionPayment.hidden = true;
    paymentReferenceField.disabled = true;

    if (paymentDateTimeField) {
        paymentDateTimeField.disabled = true;
    }

    saveNextBtn.hidden = false;
    submitAdmissionBtn.hidden = true;

    saveNextBtn.scrollIntoView({
        behavior: 'smooth',
        block: 'center'
    });
}

saveNextBtn.addEventListener('click', () => {
    const st = $('#admissionStatus');
    const file = admissionForm.elements.photo.files[0];

    if (!admissionForm.reportValidity()) return;

    if (file && file.size > 2 * 1024 * 1024) {
        return status(st, 'The photo must be smaller than 2 MB.', false);
    }

    const aadhaar = admissionForm.elements.aadhaar.value.trim();

    if (aadhaar && !/^[0-9]{12}$/.test(aadhaar)) {
        return status(st, 'Aadhaar must be exactly 12 digits, or leave it blank.', false);
    }

    status(st, 'Details checked. Now scan the institute QR and pay the ₹500 admission fee.', true);

    showPaymentStep();
});

backToDetailsBtn.addEventListener('click', showDetailsStep);

admissionForm.addEventListener('submit', async e => {
    e.preventDefault();

    const f = e.currentTarget;
    const st = $('#admissionStatus');
    const btn = submitAdmissionBtn;
    const file = f.elements.photo.files[0];

    f.querySelectorAll('.receipt-download, .receipt-download-link').forEach(el => el.remove());

    if (!admissionPayment.hidden && !f.reportValidity()) return;

    if (file && file.size > 2 * 1024 * 1024) {
        return status(st, 'The photo must be smaller than 2 MB.', false);
    }

    const aadhaar = f.elements.aadhaar.value.trim();

    if (aadhaar && !/^[0-9]{12}$/.test(aadhaar)) {
        return status(st, 'Aadhaar must be exactly 12 digits, or leave it blank.', false);
    }

    btn.disabled = true;
    btn.textContent = 'Submitting Application...';

    try {
        const r = await fetch('/api/admissions', {
            method: 'POST',
            body: new FormData(f)
        });

        const j = await r.json();

        if (!r.ok) {
            throw Error(j.error || 'The application could not be submitted.');
        }

        status(st, `${j.message} Application ID: ${j.id}.`, true);
        showBCITToast('Admission form successfully submitted!', 'success');
        const receipt = document.createElement('a');

        receipt.className = 'btn receipt-download receipt-download-link';
        receipt.textContent = 'Download Application Receipt (PDF)';
        receipt.href = j.receipt_url;
        receipt.setAttribute('download', '');

        st.insertAdjacentElement('afterend', receipt);

        // Automatically start the application-receipt PDF download.
        receipt.click();

        f.reset();

        if (paymentDateTimeField) {
            const d = new Date();

            paymentDateTimeField.value =
                `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}T${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        }

        if (admissionDateField) {
            const now = new Date();

            admissionDateField.value =
                `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
        }

        showDetailsStep();

    } catch (err) {
        status(st, 'The application could not be submitted. Please review your details and try again.', false);
        showBCITToast('The application could not be submitted. Please try again.', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Submit Admission Form & Payment Reference';
    }
});

// Student Profile receipt lookup
const studentProfileForm = $('#studentProfileForm');

if (studentProfileForm) {
    studentProfileForm.addEventListener('submit', async e => {
        e.preventDefault();

        const btn = $('#profileLookupBtn');
        const st = $('#profileStatus');
        const results = $('#profileResults');

        results.replaceChildren();

        btn.disabled = true;
        btn.textContent = 'Searching…';

        try {
            const payload = Object.fromEntries(
                new FormData(studentProfileForm).entries()
            );

            const response = await fetch('/api/student/profile', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Could not find receipts.');
            }

            status(
                st,
                `Found ${data.admissions.length} admission record(s) for ${data.student_name}.`,
                true
            );
            showBCITToast('Student profile loaded successfully.', 'success');

            data.admissions.forEach(item => {
                const card = document.createElement('div');
                card.className = 'profile-receipt-card';

                const title = document.createElement('h3');
                title.textContent = `Application #${item.id} — ${item.course}`;

                const info = document.createElement('p');

                info.textContent =
                    `Admission date: ${item.admission_date || 'Not available'} | UTR: ${item.payment_reference || 'Not provided'} | Fee: ₹${item.fee} | Payment date/time entered: ${item.payment_datetime || 'Not provided'} | Status: ${item.payment_status === 'paid' ? 'Verified / Paid' : 'Pending verification'}`;

                const link = document.createElement('a');

                link.className = 'btn';
                link.href = item.receipt_url;
                link.textContent = 'Download Admission Receipt (PDF)';
                link.setAttribute('download', '');

                card.append(title, info, link);
                results.append(card);
            });

        } catch (err) {
            status(st, 'Student profile could not be loaded. Please check your details and try again.', false);
            showBCITToast('Student profile could not be loaded. Please try again.', 'error');

        } finally {
            btn.disabled = false;
            btn.textContent = 'Find My Receipts';
        }
    });
}

// Enquiry form
$('#enquiryForm').addEventListener('submit', async e => {
    e.preventDefault();

    const f = e.currentTarget;
    const st = $('#enquiryStatus');
    const btn = f.querySelector('button[type=submit]');

    const data = Object.fromEntries(
        new FormData(f).entries()
    );

    btn.disabled = true;
    btn.textContent = 'Sending...';

    try {
        const r = await fetch('/api/enquiries', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });

        const j = await r.json();

        if (!r.ok) {
            throw Error(j.error || 'The enquiry could not be submitted.');
        }

        status(st, `${j.message} Reference ID: ${j.id}`, true);
        showBCITToast('Enquiry successfully submitted!', 'success');
        f.reset();

    } catch (err) {
        status(st, err.message + ' Please check that the server is running.', false);

    } finally {
        btn.disabled = false;
        btn.textContent = 'Send Enquiry';
    }
});

// Course information for the AI assistant
const info = {
    ADCA: 'ADCA fee: ₹8,000. Topics include computer fundamentals, Windows, MS Word, Excel, PowerPoint, internet, and database basics. Please confirm the final syllabus and duration with the institute.',

    DCA: 'DCA fee: ₹4,000. Topics include operating systems, MS Office, internet, email, and basic computer applications. Please confirm the final syllabus and duration with the institute.',

    DFA: 'DFA fee: ₹6,000. Topics include accounting concepts, vouchers, ledgers, billing, and GST basics. Please confirm the final syllabus with the institute.',

    PGDCA: 'PGDCA fee: ₹18,000. Topics include computer applications, database concepts, programming fundamentals, networking, and project work. Please confirm eligibility and duration with the institute.',

    DTTP: 'DTTP fee: ₹6,000. Topics include desktop publishing, page layout, typography, document design, and print design basics. Please confirm the final syllabus and duration with the institute.'
};

function answer(q) {
    const s = q.toLowerCase();

    if (
        s.includes('fee') ||
        s.includes('fees') ||
        s.includes('course') ||
        s.includes('price')
    ) {
        return 'BCIT Courses & Fees:\n' +
            Object.entries({
                ADCA: '₹8,000',
                DCA: '₹4,000',
                DFA: '₹6,000',
                PGDCA: '₹18,000',
                DTTP: '₹6,000'
            })
            .map(([k, v]) => k + ': ' + v)
            .join('\n') +
            '\nPlease contact the institute to confirm course duration.';
    }

    const c = Object.keys(info).find(k => s.includes(k.toLowerCase()));

    if (
        c &&
        (
            s.includes('syllabus') ||
            s.includes('topic') ||
            s.includes('learn') ||
            s.includes('about')
        )
    ) {
        return info[c];
    }

    if (s.includes('duration') || s.includes('time')) {
        return 'Please contact BCIT at +91 9708750305 to confirm the exact course duration.';
    }

    if (
        s.includes('future') ||
        s.includes('scope') ||
        s.includes('job') ||
        s.includes('career')
    ) {
        return 'These skills may be useful in computer applications, office administration, data entry, accounting support, and desktop publishing. Outcomes depend on your skills and local opportunities; employment is not guaranteed.';
    }

    if (
        s.includes('contact') ||
        s.includes('phone') ||
        s.includes('email')
    ) {
        return 'Phone: +91 9708750305 | Email: bcitcomputer.445@gmail.com';
    }

    if (
        s.includes('admission') ||
        s.includes('apply') ||
        s.includes('form')
    ) {
        return 'Open the Admission tab and complete the form. You may upload an optional JPG, PNG, or WEBP photo (maximum 2 MB).';
    }

    return 'I can help with courses, fees, syllabus, duration, career prospects, and admission. Ask about ADCA, DCA, DFA, PGDCA, or DTTP. Phone: +91 9708750305.';
}

// AI Assistant
async function ask(q) {
    if (!q.trim()) return;

    add(q, 'user');

    const wait = document.createElement('div');

    wait.className = 'bubble bot';
    wait.textContent = 'Thinking...';

    $('#chatMessages').appendChild(wait);
    $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;

    const personal =
        /(\bmy\b|mera|meri|mere|fee balance|payment status|my fees|my fee|my result|my marks|my attendance|my assignment|my study material|my notes|my submission|student id\b)/i.test(q);

    try {
        const endpoint = personal
            ? '/api/student/ai-assistant'
            : '/api/assistant';

        const r = await fetch(endpoint, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                question: q
            }),
            cache: 'no-store'
        });

        const j = await r.json().catch(() => ({}));

        wait.remove();

        if ((r.status === 401 || r.status === 403) && personal) {
            add(
                'To view your personal fees, results, attendance, assignments, and study materials, please log in through Student Profile first, then ask me here again.',
                'bot'
            );
            return;
        }

        if (r.ok && j.answer) {
            add(j.answer, 'bot');
            return;
        }

        add(j.error || 'AI Assistant is temporarily unavailable. Please try again.', 'bot');

    } catch (e) {
        wait.remove();

        add(
            'Unable to connect to the AI Assistant. Please check your internet connection and try again.',
            'bot'
        );
    }
}

function add(t, who) {
    const d = document.createElement('div');

    d.className = 'bubble ' + who;
    d.textContent = t;

    $('#chatMessages').appendChild(d);
    $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
}

$('#chatForm').addEventListener('submit', e => {
    e.preventDefault();

    const i = $('#chatInput');

    ask(i.value);
    i.value = '';
});

$$('[data-question]').forEach(b => {
    b.addEventListener('click', () => ask(b.dataset.question));
});

$$('.choose-course').forEach(b => {
    b.addEventListener('click', () => {
        $('#enquiryForm').elements.course.value = b.dataset.course;
        location.hash = '#enquiry';
    });
});

// ======================================================
// STUDENT PORTAL - UPDATED LOGIN / LOGOUT SYSTEM
// ======================================================

(() => {
    const form = document.getElementById('portalLoginForm');

    if (!form) return;

    const loginPanel = document.getElementById('studentPortalLogin');
    const loginCard = document.getElementById('portalLoginPanel');
    const statusBox = document.getElementById('portalLoginStatus');
    const embed = document.getElementById('portalEmbed');
    const frame = document.getElementById('studentPortalFrame');
    const logoutBtn = document.getElementById('portalLogoutBtn');
    const receiptForm = document.getElementById('studentProfileForm');

    if (!embed || !frame) return;

    // Remove the outer card/border around the student dashboard.
    if (loginPanel) {
        loginPanel.style.setProperty('width', '100%', 'important');
        loginPanel.style.setProperty('max-width', 'none', 'important');
        loginPanel.style.setProperty('margin', '0', 'important');
        loginPanel.style.setProperty('padding', '0', 'important');
        loginPanel.style.setProperty('border', '0', 'important');
        loginPanel.style.setProperty('border-radius', '0', 'important');
        loginPanel.style.setProperty('box-shadow', 'none', 'important');
        loginPanel.style.setProperty('background', 'transparent', 'important');
    }

    embed.style.setProperty('width', '100%', 'important');
    embed.style.setProperty('max-width', 'none', 'important');
    embed.style.setProperty('margin', '0', 'important');
    embed.style.setProperty('padding', '0', 'important');
    embed.style.setProperty('border', '0', 'important');
    embed.style.setProperty('border-radius', '0', 'important');
    embed.style.setProperty('box-shadow', 'none', 'important');
    embed.style.setProperty('background', 'transparent', 'important');

    frame.style.setProperty('display', 'block', 'important');
    frame.style.setProperty('width', '100%', 'important');
    frame.style.setProperty('max-width', 'none', 'important');
    frame.style.setProperty('border', '0', 'important');
    frame.style.setProperty('border-radius', '0', 'important');
    frame.style.setProperty('margin', '0', 'important');
    frame.style.setProperty('padding', '0', 'important');
    frame.style.setProperty('background', '#fff', 'important');

    function showStudentDashboard() {
        // After student login, hide BOTH forms: the admission receipt lookup
        // form and the student portal login form. Keep the section heading
        // and explanatory text visible above the dashboard.
        if (receiptForm) {
            receiptForm.hidden = true;
            receiptForm.style.setProperty('display', 'none', 'important');
        }
        if (statusBox) {
            statusBox.hidden = true;
            statusBox.style.setProperty('display', 'none', 'important');
        }
        if (loginCard) {
            loginCard.hidden = true;
            // Force-hide the card itself even if another stylesheet overrides
            // the browser's default [hidden] rule (prevents the empty strip).
            loginCard.style.setProperty('display', 'none', 'important');
            loginCard.style.setProperty('height', '0', 'important');
            loginCard.style.setProperty('min-height', '0', 'important');
            loginCard.style.setProperty('margin', '0', 'important');
            loginCard.style.setProperty('padding', '0', 'important');
            loginCard.style.setProperty('border', '0', 'important');
        }
        embed.hidden = false;

        // Load the dashboard only when required.
        if (!frame.getAttribute('src') || frame.getAttribute('src') === 'about:blank') {
            frame.src = frame.dataset.src || '/student/dashboard';
        }

        // Avoid an empty strip above the embedded dashboard.
        embed.style.marginTop = '0';
    }

    function showStudentLogin() {
        // Hide dashboard and restore both forms for the next login/session.
        embed.hidden = true;
        frame.src = 'about:blank';
        if (receiptForm) {
            receiptForm.hidden = false;
            receiptForm.style.removeProperty('display');
        }
        if (statusBox) {
            statusBox.hidden = false;
            statusBox.style.removeProperty('display');
        }
        if (loginCard) {
            loginCard.hidden = false;
            loginCard.style.removeProperty('display');
            loginCard.style.removeProperty('height');
            loginCard.style.removeProperty('min-height');
            loginCard.style.removeProperty('margin');
            loginCard.style.removeProperty('padding');
            loginCard.style.removeProperty('border');
        }
        form.hidden = false;
        form.reset();

        if (statusBox) {
            statusBox.textContent = 'You have been logged out.';
            statusBox.className = 'form-status';
        }
    }

    // Initially show the login card, not the dashboard.
    embed.hidden = true;
    if (loginCard) {
        loginCard.hidden = false;
        loginCard.style.removeProperty('display');
    }
    form.hidden = false;
    if (receiptForm) {
        receiptForm.hidden = false;
        receiptForm.style.removeProperty('display');
    }
    if (statusBox) {
        statusBox.hidden = false;
        statusBox.style.removeProperty('display');
    }

    // Student login
    form.addEventListener('submit', async e => {
        e.preventDefault();

        statusBox.textContent = 'Signing in…';
        statusBox.className = 'form-status';

        const loginButton = form.querySelector('button[type="submit"]');

        if (loginButton) {
            loginButton.disabled = true;
            loginButton.textContent = 'Signing In...';
        }

        try {
            const fd = new FormData(form);

            const r = await fetch('/api/student/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(
                    Object.fromEntries(fd.entries())
                ),
                cache: 'no-store'
            });

            const j = await r.json();

            if (!r.ok) {
                throw new Error(j.error || 'Login failed.');
            }

            // Clear password after successful login.
            const passwordInput = form.querySelector(
                'input[name="password"]'
            );

            if (passwordInput) {
                passwordInput.value = '';
            }

            // Hide forms and display dashboard.
            showStudentDashboard();
            showBCITToast('Login successful.', 'success');

        } catch (err) {
            statusBox.textContent = 'Login failed. Please check your Student ID and password.';
            statusBox.className = 'form-status error';
            showBCITToast('Login failed. Please check your Student ID and password.', 'error');

        } finally {
            if (loginButton) {
                loginButton.disabled = false;
                loginButton.textContent = 'Open My Student Profile';
            }
        }
    });

    // Logout from the parent Student Portal.
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async () => {
            logoutBtn.disabled = true;
            logoutBtn.textContent = 'Logging Out...';

            try {
                const response = await fetch('/student/logout', {
                    method: 'POST',
                    credentials: 'same-origin',
                    cache: 'no-store'
                });

                if (!response.ok) {
                    throw new Error('Logout request failed. Please try again.');
                }

                showStudentLogin();
                showBCITToast('Logout successful.', 'success');

            } catch (err) {
                if (statusBox) {
                    statusBox.textContent = 'Logout could not be completed. Please try again.';
                    statusBox.className = 'form-status error';
                }
                showBCITToast('Logout could not be completed. Please try again.', 'error');

            } finally {
                logoutBtn.disabled = false;
                logoutBtn.textContent = 'Logout';
            }
        });
    }
})();
