# login_csgoempire_gui.py
# Giao diện điều khiển CSGOEmpire roulette bằng Playwright với profile Chrome riêng.

import asyncio
import json
import queue
import random
import threading
import tkinter as tk
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from tkinter import messagebox, scrolledtext, ttk

DEFAULT_USER_DATA_DIR = "./chrome_profile_csgo"
SESSION_FILE = "csgoempire_session.json"
BET_OPEN_TIMEOUT_SECONDS = 15

BET_SIDE_SELECTORS = {
    "t": ".bet-btn__overlay--side-t",
    "c": ".bet-btn__overlay--side-c",
}
LAST100_COIN_SELECTORS = {
    "c": ".coin-ct",
    "dice": ".coin-bonus",
    "t": ".coin-t",
}
STRATEGY_OPTIONS = {
    "Giữ cửa bắt đầu": "fixed",
    "Random khi reset": "random_reset",
    "Bên ít ra hơn Last100": "least_last100",
}
STRATEGY_LABELS = {value: label for label, value in STRATEGY_OPTIONS.items()}


@dataclass
class BotConfig:
    user_data_dir: str
    base_bet_amount: Decimal
    bet_side: str
    strategy: str
    stop_loss: Decimal
    target_profit: Decimal
    result_timeout_seconds: int
    max_rounds: int | None


def parse_decimal(value, field_name):
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        raise ValueError(f"{field_name} không được để trống")

    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} không hợp lệ: {value!r}") from exc

    if number <= 0:
        raise ValueError(f"{field_name} phải lớn hơn 0")

    return number


def parse_balance(balance_text):
    cleaned = balance_text.strip().replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Không đọc được balance: {balance_text!r}") from exc


def format_amount(amount):
    return format(amount.quantize(Decimal("0.01")), "f")


def choose_bet_side(config, last100_counts=None):
    if config.strategy == "fixed":
        return config.bet_side

    if config.strategy == "random_reset":
        return random.choice(("c", "t"))

    if config.strategy == "least_last100":
        if not last100_counts:
            return random.choice(("c", "t"))

        c_count = last100_counts.get("c", 0)
        t_count = last100_counts.get("t", 0)

        if c_count < t_count:
            return "c"
        if t_count < c_count:
            return "t"

        return random.choice(("c", "t"))

    raise ValueError(f"Chiến thuật không hợp lệ: {config.strategy}")


async def get_balance(page, timeout=15000):
    balance_locator = page.locator('[data-testid="currency-amount"]').first
    await balance_locator.wait_for(state="visible", timeout=timeout)
    balance_text = await balance_locator.inner_text()
    return parse_balance(balance_text)


async def try_get_balance(page, timeout=4000):
    try:
        return await get_balance(page, timeout=timeout)
    except Exception:
        return None


async def get_last100_counts(page):
    counts = {}

    for side, selector in LAST100_COIN_SELECTORS.items():
        coin = page.locator(selector).first
        await coin.wait_for(state="visible", timeout=15000)
        count_text = await coin.locator(
            'xpath=following-sibling::div[contains(@class, "text-xxs")][1]'
        ).inner_text()
        counts[side] = int(count_text.strip().replace(",", ""))

    return counts


def detect_last100_result(before_counts, after_counts):
    increased_sides = [
        side
        for side, before_count in before_counts.items()
        if after_counts.get(side) == before_count + 1
    ]

    if len(increased_sides) == 1:
        return increased_sides[0]

    return None


async def wait_for_last100_result(page, before_counts, timeout_seconds, stop_event):
    for _ in range(timeout_seconds):
        if stop_event.is_set():
            return None, await get_last100_counts(page), True

        await asyncio.sleep(1)
        after_counts = await get_last100_counts(page)
        result_side = detect_last100_result(before_counts, after_counts)

        if result_side:
            return result_side, after_counts, False

    return None, await get_last100_counts(page), False


def should_stop(current_balance, start_balance, config):
    loss = start_balance - current_balance
    profit = current_balance - start_balance

    if loss >= config.stop_loss:
        return True, f"chạm stop loss: -{loss}"

    if profit >= config.target_profit:
        return True, f"chạm target: +{profit}"

    return False, ""


def get_bet_input(page):
    return page.get_by_placeholder("Enter bet amount...").or_(
        page.locator('input[inputmode="decimal"]')
    ).first


async def is_bet_input_ready(page):
    bet_input = get_bet_input(page)
    try:
        await bet_input.wait_for(state="visible", timeout=1000)
        return await bet_input.evaluate(
            """(input) => {
                const style = window.getComputedStyle(input);
                const rect = input.getBoundingClientRect();

                return rect.width > 0
                    && rect.height > 0
                    && !input.disabled
                    && !input.readOnly
                    && style.display !== "none"
                    && style.visibility !== "hidden"
                    && style.pointerEvents !== "none";
            }"""
        )
    except Exception:
        return False


async def is_bet_button_ready(page, side):
    bet_button = page.locator(BET_SIDE_SELECTORS[side]).first
    try:
        await bet_button.wait_for(state="visible", timeout=1000)
        await bet_button.click(trial=True, timeout=700)
        return await bet_button.evaluate(
            """(overlay) => {
                const blockedClassParts = [
                    "disabled",
                    "inactive",
                    "locked",
                    "loading"
                ];
                let el = overlay;

                for (let depth = 0; el && depth < 8; depth += 1, el = el.parentElement) {
                    const style = window.getComputedStyle(el);
                    const className = String(el.className || "").toLowerCase();
                    const opacity = Number.parseFloat(style.opacity || "1");

                    if (style.display === "none"
                        || style.visibility === "hidden"
                        || style.pointerEvents === "none"
                        || opacity < 0.45
                        || el.disabled
                        || el.getAttribute("aria-disabled") === "true"
                        || blockedClassParts.some((part) => className.includes(part))) {
                        return false;
                    }
                }

                const rect = overlay.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }"""
        )
    except Exception:
        return False


async def fill_bet_amount(page, amount, log=None):
    bet_input = get_bet_input(page)
    amount_text = format_amount(amount)

    await bet_input.wait_for(state="visible", timeout=1500)

    try:
        await bet_input.fill(amount_text, timeout=1500)
    except Exception:
        pass

    current_value = await bet_input.evaluate("(input) => input.value || ''")
    if current_value != amount_text:
        await bet_input.evaluate(
            """(input, value) => {
                const descriptor = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype,
                    "value"
                );

                if (descriptor && descriptor.set) {
                    descriptor.set.call(input, value);
                } else {
                    input.value = value;
                }

                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
            }""",
            amount_text,
        )

    current_value = await bet_input.evaluate("(input) => input.value || ''")
    if current_value == amount_text:
        if log:
            log(f"Đã điền số tiền bet: {amount_text}")
        return True

    if log:
        log(f"Chưa điền được số tiền bet, input hiện là: {current_value!r}")
    return False


async def wait_for_betting_window(page, side, amount, stop_event, log, timeout_seconds=BET_OPEN_TIMEOUT_SECONDS):
    log("Đang chờ cửa bet mở...")
    amount_prefilled = False

    for _ in range(timeout_seconds * 2):
        if stop_event.is_set():
            return False

        input_ready = await is_bet_input_ready(page)
        button_ready = await is_bet_button_ready(page, side)

        if input_ready and not amount_prefilled:
            amount_prefilled = await fill_bet_amount(page, amount)
            if amount_prefilled:
                log(f"Đã điền sẵn tiền cược: {format_amount(amount)}")

        if input_ready and button_ready:
            log("Cửa bet đã mở.")
            return True

        await asyncio.sleep(0.5)

    raise TimeoutError("Hết thời gian chờ cửa bet mở")


async def try_place_bet(page, amount, side, log):
    bet_button = page.locator(BET_SIDE_SELECTORS[side]).first

    try:
        amount_filled = await fill_bet_amount(page, amount, log)
        if not amount_filled:
            return False

        await bet_button.click(timeout=1500)
        log(f"Đã click ô bet: {side}")
        return True
    except Exception:
        log("Cửa bet vừa đóng hoặc nút chưa nhận click. Chờ vòng kế tiếp rồi thử lại.")
        return False


async def save_session(context, page):
    cookies = await context.cookies()
    with open(SESSION_FILE, "w") as f:
        json.dump(
            {
                "cookies": cookies,
                "url": page.url,
            },
            f,
            indent=2,
        )


async def run_bot(user_data_dir, login_event, start_event, stop_event, config_queue, publish):
    def log(message):
        publish(("log", message))

    def stats(**values):
        publish(("stats", values))

    def round_result(**values):
        publish(("round_result", values))

    context = None
    page = None

    try:
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError:
            log("Thiếu Playwright. Cài bằng: pip install playwright && playwright install chromium")
            return

        async with async_playwright() as p:
            log("Đang mở Chrome profile riêng...")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-web-security",
                    "--disable-features=BlockInsecurePrivateNetworkRequests",
                ],
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://csgoempire.com/roulette")
            log("Trình duyệt đã mở. App đang kiểm tra trạng thái đăng nhập...")
            publish(("browser_ready", None))

            balance = await try_get_balance(page)
            if balance is None:
                log("Chưa thấy balance. Hãy đăng nhập trong trình duyệt, rồi quay lại bấm 'Đã đăng nhập'.")
                publish(("login_required", None))
            else:
                log(f"Đã thấy balance: {balance}. Có thể bấm 'Farm'.")
                stats(current_balance=str(balance), status="Đã đăng nhập")
                publish(("login_ready", None))

            while not start_event.is_set():
                if stop_event.is_set():
                    log("Đã dừng trước khi bắt đầu.")
                    return

                if login_event.is_set():
                    login_event.clear()
                    log("Đang xác nhận đăng nhập...")
                    balance = await try_get_balance(page, timeout=8000)
                    if balance is None:
                        log("Vẫn chưa thấy balance. Đăng nhập xong rồi bấm 'Đã đăng nhập' lại.")
                        publish(("login_required", None))
                    else:
                        log(f"Xác nhận đăng nhập thành công. Balance: {balance}.")
                        stats(current_balance=str(balance), status="Đã đăng nhập")
                        publish(("login_ready", None))

                await asyncio.sleep(0.2)

            try:
                config = config_queue.get_nowait()
            except queue.Empty as exc:
                raise RuntimeError("Chưa có cấu hình Farm") from exc

            if config.bet_side not in ("c", "t"):
                raise ValueError('Cửa bắt đầu chỉ được là "c" hoặc "t"')
            if config.strategy not in STRATEGY_LABELS:
                raise ValueError(f"Chiến thuật không hợp lệ: {config.strategy}")

            start_balance = await get_balance(page)
            current_balance = start_balance
            current_bet_amount = config.base_bet_amount
            current_side = config.bet_side
            choose_side_on_next_round = True
            round_number = 1
            loss_streak = 0

            log(f"Balance ban đầu: {start_balance}")
            log(f"Chiến thuật chọn cửa: {STRATEGY_LABELS[config.strategy]}")
            stats(
                start_balance=str(start_balance),
                current_balance=str(current_balance),
                profit="+0.00",
                round_number="0",
                next_bet=format_amount(current_bet_amount),
                loss_streak="0",
                last_result="-",
                status="Đang chạy",
            )

            while not stop_event.is_set():
                if config.max_rounds is not None and round_number > config.max_rounds:
                    log(f"Dừng vì đã đủ {config.max_rounds} ván.")
                    break

                current_balance = await get_balance(page)
                stop_now, stop_reason = should_stop(current_balance, start_balance, config)
                if stop_now:
                    log(f"Dừng trước ván {round_number}, {stop_reason}")
                    break

                if current_balance < current_bet_amount:
                    log(
                        f"Dừng trước ván {round_number}, balance {current_balance} "
                        f"không đủ cược {format_amount(current_bet_amount)}"
                    )
                    break

                placed = False
                last100_before = None

                while not placed and not stop_event.is_set():
                    stats(
                        round_number=str(round_number),
                        next_bet=format_amount(current_bet_amount),
                        status="Chờ cửa bet",
                    )

                    ready = await wait_for_betting_window(
                        page,
                        current_side,
                        current_bet_amount,
                        stop_event,
                        log,
                    )
                    if not ready:
                        break

                    last100_before = await get_last100_counts(page)
                    if choose_side_on_next_round:
                        current_side = choose_bet_side(config, last100_before)
                        choose_side_on_next_round = False
                        log(f"Chọn cửa cho chu kỳ mới: {current_side}")

                        ready = await wait_for_betting_window(
                            page,
                            current_side,
                            current_bet_amount,
                            stop_event,
                            log,
                        )
                        if not ready:
                            break

                        last100_before = await get_last100_counts(page)
                        if config.strategy == "least_last100":
                            refreshed_side = choose_bet_side(config, last100_before)
                            if refreshed_side != current_side:
                                current_side = refreshed_side
                                log(f"Đổi cửa theo Last100 mới nhất: {current_side}")

                    log("")
                    log(f"=== Ván {round_number} ===")
                    log(f"Last 100 trước ván: {last100_before}")
                    log(f"Cược {format_amount(current_bet_amount)} vào cửa {current_side}")

                    stats(
                        round_number=str(round_number),
                        next_bet=format_amount(current_bet_amount),
                        last100=f"C: {last100_before['c']} | Dice: {last100_before['dice']} | T: {last100_before['t']}",
                        status="Đang đặt cược",
                    )

                    placed = await try_place_bet(page, current_bet_amount, current_side, log)
                    if not placed:
                        choose_side_on_next_round = loss_streak == 0
                        await asyncio.sleep(0.5)

                if stop_event.is_set():
                    log("Đã nhận lệnh dừng.")
                    break

                if not placed or last100_before is None:
                    log("Dừng, chưa đặt được cược vì cửa bet chưa sẵn sàng.")
                    break

                result_side, last100_after, stopped = await wait_for_last100_result(
                    page,
                    last100_before,
                    config.result_timeout_seconds,
                    stop_event,
                )
                log(f"Last 100 sau ván: {last100_after}")

                if stopped:
                    log("Đã nhận lệnh dừng.")
                    break

                if not result_side:
                    log("Dừng, chưa xác định được kết quả từ Last 100.")
                    break

                won = result_side == current_side
                result_status = "WIN" if won else "LOSS"
                log(f"Kết quả: {result_side} => {result_status}")

                current_balance = await get_balance(page)
                profit = current_balance - start_balance
                log(f"Balance hiện tại: {current_balance} ({profit:+})")

                round_result(
                    round_number=round_number,
                    bet_side=current_side,
                    result_side=result_side,
                    won=won,
                    profit=f"{profit:+}",
                    balance=str(current_balance),
                    bet_amount=format_amount(current_bet_amount),
                )

                if won:
                    current_bet_amount = config.base_bet_amount
                    loss_streak = 0
                    choose_side_on_next_round = True
                else:
                    current_bet_amount *= 2
                    loss_streak += 1

                stats(
                    current_balance=str(current_balance),
                    profit=f"{profit:+}",
                    next_bet=format_amount(current_bet_amount),
                    loss_streak=str(loss_streak),
                    last_result=f"{result_side} / {result_status}",
                    last100=f"C: {last100_after['c']} | Dice: {last100_after['dice']} | T: {last100_after['t']}",
                    status="Đang chạy",
                )

                stop_now, stop_reason = should_stop(current_balance, start_balance, config)
                if stop_now:
                    log(f"Dừng sau ván {round_number}, {stop_reason}")
                    break

                round_number += 1

            if page and context:
                try:
                    await save_session(context, page)
                    log(f"Session lưu vào {SESSION_FILE}")
                except Exception:
                    log("Bỏ qua lưu session vì trình duyệt đã đóng.")

    except Exception as exc:
        log(f"Lỗi: {exc}")
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        publish(("done", None))


class EmpireBotApp:
    def __init__(self, root):
        self.root = root
        self.root.title("CSGOEmpire Roulette Bot")
        self.root.geometry("1120x820")
        self.root.minsize(980, 700)

        self.events = queue.Queue()
        self.config_queue = queue.Queue()
        self.worker_thread = None
        self.login_event = threading.Event()
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.browser_ready = False
        self.login_ready = False
        self.result_history = []

        self.colors = {
            "bg": "#101114",
            "panel": "#181a20",
            "panel_2": "#20232b",
            "text": "#f4f1e8",
            "muted": "#aaa6a0",
            "accent": "#e7b84b",
            "green": "#37c686",
            "red": "#ef6f6c",
            "line": "#30343d",
            "input": "#111318",
        }

        self.root.configure(bg=self.colors["bg"])
        self._setup_styles()
        self._build_ui()
        self._set_idle_state()
        self.root.after(100, self._process_events)
        self.root.after(350, self._open_browser)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "TRadiobutton",
            background=self.colors["panel"],
            foreground=self.colors["text"],
            font=("Helvetica", 12),
        )
        style.map(
            "TRadiobutton",
            background=[("active", self.colors["panel"])],
            foreground=[("active", self.colors["accent"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.colors["input"],
            background=self.colors["input"],
            foreground=self.colors["text"],
            arrowcolor=self.colors["accent"],
            bordercolor=self.colors["line"],
            lightcolor=self.colors["line"],
            darkcolor=self.colors["line"],
            font=("Helvetica", 13),
        )

    def _build_ui(self):
        header = tk.Frame(self.root, bg=self.colors["bg"])
        header.pack(fill="x", padx=24, pady=(20, 12))

        tk.Label(
            header,
            text="CSGOEmpire Roulette Bot",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Helvetica", 24, "bold"),
        ).pack(side="left")

        self.status_badge = tk.Label(
            header,
            text="Chưa mở",
            bg=self.colors["panel_2"],
            fg=self.colors["accent"],
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=8,
        )
        self.status_badge.pack(side="right")

        content = tk.Frame(self.root, bg=self.colors["bg"])
        content.pack(fill="both", expand=True, padx=24, pady=(0, 24))

        left = tk.Frame(content, bg=self.colors["panel"], padx=18, pady=18)
        left.pack(side="left", fill="y")

        right = tk.Frame(content, bg=self.colors["bg"])
        right.pack(side="right", fill="both", expand=True, padx=(18, 0))

        self._build_config_panel(left)
        self._build_dashboard(right)
        self._build_result_chart(right)
        self._build_log(right)

    def _build_config_panel(self, parent):
        tk.Label(
            parent,
            text="Cấu hình",
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w")

        self.base_bet_var = tk.StringVar(value="1.00")
        self.stop_loss_var = tk.StringVar(value="5.00")
        self.target_profit_var = tk.StringVar(value="10.00")
        self.timeout_var = tk.StringVar(value="5")
        self.max_rounds_var = tk.StringVar(value="")
        self.profile_var = tk.StringVar(value=DEFAULT_USER_DATA_DIR)
        self.side_var = tk.StringVar(value="t")
        self.strategy_var = tk.StringVar(value="Random khi reset")

        self.input_widgets = []
        self._entry(parent, "Mức cược gốc", self.base_bet_var)
        self._entry(parent, "Stop loss", self.stop_loss_var)
        self._entry(parent, "Target profit", self.target_profit_var)
        self._entry(parent, "Đợi kết quả sau cược (giây)", self.timeout_var)
        self._entry(parent, "Số ván tối đa", self.max_rounds_var, placeholder="Trống = không giới hạn")
        self.profile_entry = self._entry(parent, "Chrome profile", self.profile_var)
        self._strategy_select(parent)

        tk.Label(
            parent,
            text="Cửa bắt đầu",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Helvetica", 11, "bold"),
        ).pack(anchor="w", pady=(14, 6))

        side_frame = tk.Frame(parent, bg=self.colors["panel"])
        side_frame.pack(fill="x")
        self.c_radio = ttk.Radiobutton(side_frame, text="C", value="c", variable=self.side_var)
        self.t_radio = ttk.Radiobutton(side_frame, text="T", value="t", variable=self.side_var)
        self.c_radio.pack(side="left", padx=(0, 18))
        self.t_radio.pack(side="left")
        self.input_widgets.extend([self.c_radio, self.t_radio])

        button_frame = tk.Frame(parent, bg=self.colors["panel"])
        button_frame.pack(fill="x", pady=(22, 0))

        self.login_button = self._button(
            button_frame,
            "Đã đăng nhập",
            self._confirm_login,
            bg=self.colors["accent"],
            fg="#111111",
        )
        self.login_button.pack(fill="x", pady=(0, 10))

        self.farm_button = self._button(
            button_frame,
            "Farm",
            self._start_bot,
            bg=self.colors["green"],
            fg="#0d1711",
        )
        self.farm_button.pack(fill="x", pady=(0, 10))

        self.stop_button = self._button(
            button_frame,
            "Dừng",
            self._stop_bot,
            bg=self.colors["red"],
            fg="#170d0d",
        )
        self.stop_button.pack(fill="x")

    def _build_dashboard(self, parent):
        cards = tk.Frame(parent, bg=self.colors["bg"])
        cards.pack(fill="x")

        self.stat_vars = {
            "start_balance": tk.StringVar(value="-"),
            "current_balance": tk.StringVar(value="-"),
            "profit": tk.StringVar(value="-"),
            "round_number": tk.StringVar(value="-"),
            "next_bet": tk.StringVar(value="-"),
            "loss_streak": tk.StringVar(value="-"),
            "last_result": tk.StringVar(value="-"),
            "last100": tk.StringVar(value="-"),
        }

        card_specs = [
            ("Balance đầu", "start_balance"),
            ("Balance hiện tại", "current_balance"),
            ("Lời / lỗ", "profit"),
            ("Ván", "round_number"),
            ("Cược kế tiếp", "next_bet"),
            ("Chuỗi thua", "loss_streak"),
            ("Kết quả mới", "last_result"),
            ("Last 100", "last100"),
        ]

        for index, (label, key) in enumerate(card_specs):
            card = tk.Frame(cards, bg=self.colors["panel"], padx=14, pady=12)
            card.grid(row=index // 4, column=index % 4, sticky="nsew", padx=6, pady=6)
            cards.grid_columnconfigure(index % 4, weight=1)

            tk.Label(
                card,
                text=label,
                bg=self.colors["panel"],
                fg=self.colors["muted"],
                font=("Helvetica", 10, "bold"),
            ).pack(anchor="w")
            tk.Label(
                card,
                textvariable=self.stat_vars[key],
                bg=self.colors["panel"],
                fg=self.colors["text"],
                font=("Helvetica", 14, "bold"),
                wraplength=180,
                justify="left",
            ).pack(anchor="w", pady=(8, 0))

    def _build_log(self, parent):
        log_frame = tk.Frame(parent, bg=self.colors["panel"], padx=14, pady=14)
        log_frame.pack(fill="both", expand=True, pady=(18, 0))

        tk.Label(
            log_frame,
            text="Log",
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=("Helvetica", 16, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        self.log_box = scrolledtext.ScrolledText(
            log_frame,
            bg="#0c0d10",
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            font=("Menlo", 12),
            padx=12,
            pady=12,
            wrap="word",
            height=18,
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def _build_result_chart(self, parent):
        chart_frame = tk.Frame(parent, bg=self.colors["panel"], padx=14, pady=14)
        chart_frame.pack(fill="x", pady=(18, 0))

        header = tk.Frame(chart_frame, bg=self.colors["panel"])
        header.pack(fill="x", pady=(0, 10))

        tk.Label(
            header,
            text="Win/Loss & Kết quả",
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=("Helvetica", 16, "bold"),
        ).pack(side="left")

        self.chart_summary_var = tk.StringVar(value="W 0 / L 0")
        tk.Label(
            header,
            textvariable=self.chart_summary_var,
            bg=self.colors["panel_2"],
            fg=self.colors["accent"],
            font=("Helvetica", 11, "bold"),
            padx=12,
            pady=6,
        ).pack(side="right")

        self.result_canvas = tk.Canvas(
            chart_frame,
            height=190,
            bg="#0c0d10",
            highlightthickness=0,
            relief="flat",
        )
        self.result_canvas.pack(fill="x")
        self.result_canvas.bind("<Configure>", lambda _event: self._draw_result_chart())
        self._draw_result_chart()

    def _strategy_select(self, parent):
        tk.Label(
            parent,
            text="Chiến thuật chọn cửa",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Helvetica", 11, "bold"),
        ).pack(anchor="w", pady=(14, 6))

        combo = ttk.Combobox(
            parent,
            textvariable=self.strategy_var,
            values=list(STRATEGY_OPTIONS.keys()),
            state="readonly",
            font=("Helvetica", 13),
            width=24,
        )
        combo.pack(fill="x", ipady=6)
        self.input_widgets.append(combo)

    def _entry(self, parent, label, variable, placeholder=None):
        tk.Label(
            parent,
            text=label,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Helvetica", 11, "bold"),
        ).pack(anchor="w", pady=(14, 6))

        entry = tk.Entry(
            parent,
            textvariable=variable,
            bg=self.colors["input"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            font=("Helvetica", 13),
            width=26,
        )
        entry.pack(fill="x", ipady=9)
        self.input_widgets.append(entry)

        if placeholder:
            tk.Label(
                parent,
                text=placeholder,
                bg=self.colors["panel"],
                fg=self.colors["muted"],
                font=("Helvetica", 9),
            ).pack(anchor="w", pady=(4, 0))

        return entry

    def _button(self, parent, text, command, bg, fg):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=10,
            cursor="hand2",
        )

    def _read_config(self):
        timeout = self.timeout_var.get().strip()
        max_rounds = self.max_rounds_var.get().strip()
        strategy = STRATEGY_OPTIONS.get(self.strategy_var.get())

        if not timeout.isdigit() or int(timeout) <= 0:
            raise ValueError("Thời gian đợi kết quả phải là số nguyên lớn hơn 0")
        if not strategy:
            raise ValueError("Chiến thuật chọn cửa không hợp lệ")

        parsed_max_rounds = None
        if max_rounds:
            if not max_rounds.isdigit() or int(max_rounds) <= 0:
                raise ValueError("Số ván tối đa phải là số nguyên lớn hơn 0")
            parsed_max_rounds = int(max_rounds)

        return BotConfig(
            user_data_dir=self.profile_var.get().strip() or DEFAULT_USER_DATA_DIR,
            base_bet_amount=parse_decimal(self.base_bet_var.get(), "Mức cược gốc"),
            bet_side=self.side_var.get(),
            strategy=strategy,
            stop_loss=parse_decimal(self.stop_loss_var.get(), "Stop loss"),
            target_profit=parse_decimal(self.target_profit_var.get(), "Target profit"),
            result_timeout_seconds=int(timeout),
            max_rounds=parsed_max_rounds,
        )

    def _open_browser(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        user_data_dir = self.profile_var.get().strip() or DEFAULT_USER_DATA_DIR
        self._clear_config_queue()
        self.login_event.clear()
        self.start_event.clear()
        self.stop_event.clear()
        self.browser_ready = False
        self.login_ready = False
        self._clear_log()
        self._clear_result_chart()
        self._append_log("Tự động mở trình duyệt...")
        self._set_launching_state()

        self.worker_thread = threading.Thread(
            target=self._run_worker,
            args=(user_data_dir,),
            daemon=True,
        )
        self.worker_thread.start()

    def _run_worker(self, user_data_dir):
        asyncio.run(
            run_bot(
                user_data_dir,
                self.login_event,
                self.start_event,
                self.stop_event,
                self.config_queue,
                self.events.put,
            )
        )

    def _clear_config_queue(self):
        try:
            while True:
                self.config_queue.get_nowait()
        except queue.Empty:
            pass

    def _confirm_login(self):
        if not self.browser_ready:
            messagebox.showinfo("Chưa mở trình duyệt", "Đợi app mở trình duyệt xong rồi xác nhận.")
            return

        self.login_event.set()
        self._append_log("Đã bấm xác nhận đăng nhập. App đang kiểm tra balance...")
        self.status_badge.configure(text="Kiểm tra login", fg=self.colors["accent"])

    def _start_bot(self):
        if not self.login_ready:
            messagebox.showinfo("Chưa đăng nhập", "Đăng nhập trên trình duyệt rồi bấm 'Đã đăng nhập' trước.")
            return

        try:
            config = self._read_config()
        except ValueError as exc:
            messagebox.showerror("Cấu hình chưa đúng", str(exc))
            return

        self._clear_config_queue()
        self.config_queue.put(config)
        self.start_event.set()
        self._append_log("Farm bắt đầu.")
        self._set_running_state()

    def _stop_bot(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            self._append_log("Đang yêu cầu dừng...")
            self.status_badge.configure(text="Đang dừng", fg=self.colors["red"])

    def _process_events(self):
        try:
            while True:
                event_type, payload = self.events.get_nowait()

                if event_type == "log":
                    self._append_log(payload)
                elif event_type == "stats":
                    self._update_stats(payload)
                elif event_type == "round_result":
                    self._record_round_result(payload)
                elif event_type == "browser_ready":
                    self.browser_ready = True
                    self._set_browser_ready_state()
                elif event_type == "login_required":
                    self.login_ready = False
                    self._set_login_required_state()
                elif event_type == "login_ready":
                    self.login_ready = True
                    self._set_login_ready_state()
                elif event_type == "done":
                    self._set_done_state()
        except queue.Empty:
            pass

        self.root.after(100, self._process_events)

    def _update_stats(self, values):
        status = values.pop("status", None)
        if status:
            self.status_badge.configure(text=status, fg=self.colors["green"])

        for key, value in values.items():
            if key in self.stat_vars:
                self.stat_vars[key].set(value)

    def _record_round_result(self, payload):
        self.result_history.append(payload)
        self.result_history = self.result_history[-48:]
        self._draw_result_chart()

    def _clear_result_chart(self):
        self.result_history.clear()
        if hasattr(self, "chart_summary_var"):
            self.chart_summary_var.set("W 0 / L 0")
        if hasattr(self, "result_canvas"):
            self._draw_result_chart()

    def _draw_result_chart(self):
        if not hasattr(self, "result_canvas"):
            return

        canvas = self.result_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 560)
        height = max(canvas.winfo_height(), 180)
        pad_x = 36
        top = 28
        profit_bottom = height - 62
        result_y = height - 31

        canvas.create_line(
            pad_x,
            profit_bottom,
            width - pad_x,
            profit_bottom,
            fill=self.colors["line"],
        )
        canvas.create_text(
            pad_x,
            14,
            text="Profit",
            fill=self.colors["muted"],
            anchor="w",
            font=("Helvetica", 10, "bold"),
        )
        canvas.create_text(
            pad_x,
            result_y,
            text="KQ",
            fill=self.colors["muted"],
            anchor="e",
            font=("Helvetica", 10, "bold"),
        )

        if not self.result_history:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Chưa có dữ liệu ván",
                fill=self.colors["muted"],
                font=("Helvetica", 13, "bold"),
            )
            return

        wins = sum(1 for item in self.result_history if item.get("won"))
        losses = len(self.result_history) - wins
        self.chart_summary_var.set(f"W {wins} / L {losses}")

        profits = []
        for item in self.result_history:
            try:
                profits.append(Decimal(str(item.get("profit", "0"))))
            except InvalidOperation:
                profits.append(Decimal("0"))

        min_profit = min(min(profits), Decimal("0"))
        max_profit = max(max(profits), Decimal("0"))
        profit_span = max_profit - min_profit
        if profit_span == 0:
            profit_span = Decimal("1")

        chart_height = max(profit_bottom - top, 1)

        def map_profit(value):
            ratio = float((value - min_profit) / profit_span)
            return profit_bottom - (ratio * chart_height)

        zero_y = map_profit(Decimal("0"))
        canvas.create_line(
            pad_x,
            zero_y,
            width - pad_x,
            zero_y,
            fill=self.colors["line"],
            dash=(4, 4),
        )
        canvas.create_text(
            width - pad_x + 6,
            zero_y,
            text="0",
            fill=self.colors["muted"],
            anchor="w",
            font=("Helvetica", 9),
        )

        n = len(self.result_history)
        usable_width = max(width - (pad_x * 2), 1)
        step = usable_width / max(n - 1, 1)
        points = []

        for index, (item, profit) in enumerate(zip(self.result_history, profits)):
            x = pad_x + (step * index if n > 1 else usable_width / 2)
            y = map_profit(profit)
            points.append((x, y))

            won = bool(item.get("won"))
            result_side = str(item.get("result_side", "-")).upper()
            bet_side = str(item.get("bet_side", "-")).upper()
            marker_color = self.colors["green"] if won else self.colors["red"]

            canvas.create_line(x, result_y - 16, x, result_y + 16, fill=self.colors["line"])
            canvas.create_oval(
                x - 11,
                result_y - 11,
                x + 11,
                result_y + 11,
                fill=marker_color,
                outline="",
            )
            canvas.create_text(
                x,
                result_y,
                text=result_side[:1],
                fill="#101114",
                font=("Helvetica", 9, "bold"),
            )

            if index == n - 1 or index % 6 == 0:
                canvas.create_text(
                    x,
                    result_y + 22,
                    text=f"{item.get('round_number', index + 1)}:{bet_side}",
                    fill=self.colors["muted"],
                    font=("Helvetica", 8),
                )

        if len(points) > 1:
            flattened = [coordinate for point in points for coordinate in point]
            canvas.create_line(
                *flattened,
                fill=self.colors["accent"],
                width=2,
                smooth=True,
            )

        for index, (x, y) in enumerate(points):
            canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=self.colors["accent"], outline="")
            if index == len(points) - 1:
                canvas.create_text(
                    x + 8,
                    y,
                    text=str(self.result_history[index].get("profit", "")),
                    fill=self.colors["accent"],
                    anchor="w",
                    font=("Helvetica", 10, "bold"),
                )

    def _append_log(self, message):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _set_inputs_state(self, state):
        for widget in self.input_widgets:
            if isinstance(widget, ttk.Combobox) and state == "normal":
                widget.configure(state="readonly")
            else:
                widget.configure(state=state)

    def _set_idle_state(self):
        self.browser_ready = False
        self.login_ready = False
        self.status_badge.configure(text="Chưa mở", fg=self.colors["accent"])
        self.login_button.configure(text="Đã đăng nhập", command=self._confirm_login)
        self.login_button.configure(state="disabled")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self._set_inputs_state("normal")

    def _set_launching_state(self):
        self.status_badge.configure(text="Đang mở", fg=self.colors["accent"])
        self.login_button.configure(text="Đã đăng nhập", command=self._confirm_login)
        self.login_button.configure(state="disabled")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.profile_entry.configure(state="disabled")

    def _set_browser_ready_state(self):
        self.status_badge.configure(text="Kiểm tra login", fg=self.colors["accent"])
        self.login_button.configure(state="disabled")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def _set_login_required_state(self):
        self.status_badge.configure(text="Cần đăng nhập", fg=self.colors["red"])
        self.login_button.configure(text="Đã đăng nhập", command=self._confirm_login)
        self.login_button.configure(state="normal")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def _set_login_ready_state(self):
        self.status_badge.configure(text="Đã đăng nhập", fg=self.colors["green"])
        self.login_button.configure(text="Đã đăng nhập", command=self._confirm_login)
        self.login_button.configure(state="normal")
        self.farm_button.configure(state="normal")
        self.stop_button.configure(state="normal")

    def _set_running_state(self):
        self.status_badge.configure(text="Farm", fg=self.colors["green"])
        self.login_button.configure(state="disabled")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_inputs_state("disabled")

    def _set_done_state(self):
        self.browser_ready = False
        self.login_ready = False
        self.status_badge.configure(text="Đã dừng", fg=self.colors["accent"])
        self.login_button.configure(text="Mở lại trình duyệt", command=self._open_browser)
        self.login_button.configure(state="normal")
        self.farm_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self._set_inputs_state("normal")

    def _on_close(self):
        self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    EmpireBotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
