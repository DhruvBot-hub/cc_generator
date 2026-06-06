import os
import random
import asyncio
import io
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Load environment variables from .env file
load_dotenv()

# --- CONFIGURATION ---
# Read token from env var `TG_TOKEN` or .env file
TOKEN = os.getenv('TG_TOKEN')
ADMIN_ID = 1271128089  # Replace with your actual numeric Telegram ID

# Conversation states
ASK_CARD_DETAILS, ASK_QUANTITY = range(2)

# BIN metadata lookup removed — generator-only bot

# --- LUHN ALGORITHM GENERATOR ---
def luhn_checksum(card_number):
    digits = [int(d) for d in card_number]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    return total % 10

def generate_cc(bin_num, month, year, cvv_input):
    """Generates card data based on input parameters."""
    # Normalize special markers
    special_months = ['rnd', 'xx', 'x']
    special_years = ['rnd', 'xx', 'x', 'xxxx']

    # Expand provided month/year if present; otherwise leave None to pick randomly
    gen_month = None if month.lower() in special_months else month.zfill(2)
    if year.lower() in special_years:
        gen_year = None
    else:
        gen_year = year
        if len(gen_year) == 2:
            gen_year = "20" + gen_year

    # Ensure expiry is after June 2026. If either component is random, pick until condition satisfied.
    while True:
        y = int(gen_year) if gen_year is not None else random.randint(2026, 2032)
        m = int(gen_month) if gen_month is not None else random.randint(1, 12)
        if y > 2026 or (y == 2026 and m > 6):
            gen_year = str(y)
            gen_month = f"{m:02d}"
            break
        
    # Handle CVV
    if cvv_input.lower() in ['rnd', 'xxx', 'x']:
        gen_cvv = f"{random.randint(1000, 9999):04d}" if bin_num.startswith('3') else f"{random.randint(100, 999):03d}"
    else:
        gen_cvv = cvv_input
    
    # Build card number
    target_len = max(16, len(bin_num)) if len(bin_num) >= 12 else 16
    cc_base = ''.join(str(random.randint(0, 9)) if c.lower() == 'x' else c for c in bin_num)
    cc_base += ''.join(str(random.randint(0, 9)) for _ in range(target_len - len(cc_base) - 1))
    cc_base += '0'
    
    # Calculate Luhn checksum
    digits = [int(d) for d in cc_base]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    check_digit = (10 - (total % 10)) % 10
    
    final_cc = cc_base[:-1] + str(check_digit)
    return f"{final_cc}|{gen_month}|{gen_year}|{gen_cvv}"


# Process pool for parallel generation across CPU cores
_WORKER_COUNT = max(1, (os.cpu_count() or 2) - 1)
_GEN_EX = ProcessPoolExecutor(max_workers=_WORKER_COUNT)


def generate_chunk_sync(bin_num, month, year, cvv_input, count):
    """Synchronous chunk generator used in ProcessPoolExecutor."""
    lines = []
    for _ in range(count):
        lines.append(generate_cc(bin_num, month, year, cvv_input))
    return "\n".join(lines) + "\n"

# --- TELEGRAM BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 Welcome to the CC Generator Bot!\n\n"
        "Use `/gen` to start generating cards.\n"
        "Use `/split quantity` to split the files"
        "Use `/filter` to remove duplicates"
        "Example commands:\n"
        "• `/gen 400012xxxxxxxxx|12|28|000`\n"
        "• `/split 5000` \n"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Allow optional BIN provided as command argument: /gen 400012xxxx|MM|YY
    if context.args:
        # Join args and check if last arg is a quantity so we can generate immediately.
        joined = ' '.join(context.args).strip()
        # If user provided quantity as separate last arg: /gen BIN QTY
        if len(context.args) >= 2 and context.args[-1].isdigit():
            qty = int(context.args[-1])
            bin_text = ' '.join(context.args[:-1]).strip()
            parts = bin_text.split('|')
            bin_num = parts[0]
            month = parts[1] if len(parts) > 1 else 'rnd'
            year = parts[2] if len(parts) > 2 else 'rnd'

            # Basic validation for BIN and expiry (reuse same rules as ask_card_details)
            clean_bin_check = bin_num.lower().replace('x', '')
            if not clean_bin_check.isdigit() or not (6 <= len(bin_num) <= 16):
                await update.message.reply_text("❌ BIN must be 6-16 characters (digits or 'x')")
                return ASK_CARD_DETAILS

            special_months = ['rnd', 'xx', 'x']
            special_years = ['rnd', 'xx', 'x', 'xxxx']

            def expand_year(y: str):
                y = y.strip()
                if len(y) == 2 and y.isdigit():
                    return int("20" + y)
                try:
                    return int(y)
                except Exception:
                    return None

            if month.lower() not in special_months and year.lower() not in special_years:
                exp_year = expand_year(year)
                exp_month = int(month)
                if exp_year is None or exp_year < 2026 or (exp_year == 2026 and exp_month <= 6):
                    await update.message.reply_text("❌ Expiry must be after June 2026 (month/year must be > 06/2026)")
                    return ASK_CARD_DETAILS

            # Proceed to generate immediately (no extra prompts). Default CVV = 'rnd'
            return await generate_and_send_cards(update, context, bin_num, month, year, 'rnd', qty)

        # Otherwise store BIN and prompt for quantity as before
        parts = joined.split('|')
        bin_num = parts[0]
        month = parts[1] if len(parts) > 1 else 'rnd'
        year = parts[2] if len(parts) > 2 else 'rnd'

        # Store and prompt for quantity
        context.user_data['bin_num'] = bin_num
        context.user_data['month'] = month
        context.user_data['year'] = year

        await update.message.reply_text(
            "✅ Got it: `{}|{}|{}`\n\nNow send the **quantity** (1-10000):".format(
                bin_num,
                month,
                year,
            ),
            parse_mode="Markdown"
        )
        return ASK_QUANTITY

    await update.message.reply_text(
        "Send your BIN details in one of these formats:\n"
        "• `BIN`\n"
        "• `BIN|MM`\n"
        "• `BIN|MM|YY`\n"
        "\nExample: `400012xxxxxxxxx|12|28`",
        parse_mode="Markdown"
    )
    return ASK_CARD_DETAILS


async def ask_card_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First step: collect BIN|MM|YY (MM and YY optional, default to 'rnd')"""
    text = update.message.text.strip()
    
    # Split input
    parts = text.split('|')
    
    if len(parts) == 0 or len(parts) > 3:
        await update.message.reply_text(
            "❌ Invalid format!\nSend: **BIN** or **BIN|MM** or **BIN|MM|YY**\n"
            "Example: `400012xxxxxxxxx` (month/year will be random)",
            parse_mode="Markdown"
        )
        return ASK_CARD_DETAILS
    
    bin_num = parts[0]
    month = parts[1] if len(parts) > 1 else 'rnd'
    year = parts[2] if len(parts) > 2 else 'rnd'
    
    # Validate BIN
    clean_bin_check = bin_num.lower().replace('x', '')
    if not clean_bin_check.isdigit() or not (6 <= len(bin_num) <= 16):
        await update.message.reply_text("❌ BIN must be 6-16 characters (digits or 'x')")
        return ASK_CARD_DETAILS

    # Validate month/year if user provided them (ensure expiry > June 2026)
    special_months = ['rnd', 'xx', 'x']
    special_years = ['rnd', 'xx', 'x', 'xxxx']

    def expand_year(y: str):
        y = y.strip()
        if len(y) == 2 and y.isdigit():
            return int("20" + y)
        try:
            return int(y)
        except Exception:
            return None

    # If month is explicit, ensure it's numeric 1-12
    if month.lower() not in special_months:
        if not month.isdigit() or not (1 <= int(month) <= 12):
            await update.message.reply_text("❌ Month must be 01-12 or 'rnd' (or use 'x')")
            return ASK_CARD_DETAILS

    # If year is explicit, ensure it's a valid year
    if year.lower() not in special_years:
        exp_year = expand_year(year)
        if exp_year is None:
            await update.message.reply_text("❌ Year must be numeric (YY or YYYY) or 'rnd' (or use 'x')")
            return ASK_CARD_DETAILS

    # If both month and year provided explicitly, validate the expiry > June 2026
    if month.lower() not in special_months and year.lower() not in special_years:
        exp_year = expand_year(year)
        exp_month = int(month)
        if exp_year < 2026 or (exp_year == 2026 and exp_month <= 6):
            await update.message.reply_text("❌ Expiry must be after June 2026 (month/year must be > 06/2026)")
            return ASK_CARD_DETAILS
    
    # Store in context for next step
    context.user_data['bin_num'] = bin_num
    context.user_data['month'] = month
    context.user_data['year'] = year

    await update.message.reply_text(
        "✅ Got it: `{}|{}|{}`\n\nNow send the **quantity** (1-50000):".format(
            bin_num,
            month,
            year,
        ),
        parse_mode="Markdown"
    )
    return ASK_QUANTITY


async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Second step: collect CVV and quantity"""
    text = update.message.text.strip()
    
    # Parse quantity and optional CVV
    parts = text.split('|')
    
    if len(parts) == 1:
        qty_str = parts[0]
        cvv_input = 'rnd'
    elif len(parts) == 2:
        cvv_input, qty_str = parts
    else:
        await update.message.reply_text(
            "❌ Invalid format!\nSend just a number (e.g., `5`)\nor CVV|Quantity (e.g., `123|5`)",
            parse_mode="Markdown"
        )
        return ASK_QUANTITY
    
    try:
        qty = int(qty_str)
        if qty <= 0 or qty > 50000:
            await update.message.reply_text("❌ Quantity must be 1-50000")
            return ASK_QUANTITY
    except ValueError:
        await update.message.reply_text("❌ Quantity must be a valid number")
        return ASK_QUANTITY
    
    bin_num = context.user_data['bin_num']
    month = context.user_data['month']
    year = context.user_data['year']
    
    # Get user info
    user = update.effective_user
    user_id = user.id
    username = user.username or "N/A"
    
    # Generate and send
    return await generate_and_send_cards(update, context, bin_num, month, year, cvv_input, qty)


async def generate_and_send_cards(update: Update, context: ContextTypes.DEFAULT_TYPE, bin_num: str, month: str, year: str, cvv_input: str, qty: int, split_lines: int = None):
    """Helper: generate `qty` cards and send as a single .txt document or multiple when split_lines provided.

    Optimized: accumulate lines in a buffer and flush to BytesIO in batches to reduce I/O and improve speed.
    """
    # Use parallel generation in process pool, writing results to a temporary file to reduce memory
    loop = asyncio.get_running_loop()
    # Choose chunk size: larger chunks reduce IPC overhead; tuned for responsiveness
    chunk_size = 5000
    chunks = []
    remaining = qty
    while remaining > 0:
        this = chunk_size if remaining >= chunk_size else remaining
        chunks.append(this)
        remaining -= this

    # Schedule generation tasks in the process pool
    tasks = [loop.run_in_executor(_GEN_EX, generate_chunk_sync, bin_num, month, year, cvv_input, c) for c in chunks]
    # Gather results in order and stream to a temp file
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        for result in await asyncio.gather(*tasks):
            # result is a str with trailing newline
            tmp.write(result.encode())
        tmp.flush()
        tmp.seek(0)

        if split_lines and split_lines > 0:
            content = tmp.read().decode(errors='replace')
            lines = content.splitlines()
            total = len(lines)
            parts = (total + split_lines - 1) // split_lines
            base_root = f"{bin_num}_{qty}"
            for idx in range(parts):
                start = idx * split_lines
                part_lines = lines[start:start + split_lines]
                part_bio = io.BytesIO(("\n".join(part_lines) + "\n").encode())
                part_index = idx + 1
                filename = f"{base_root}_split_{part_index}_of_{parts}.txt"
                part_bio.name = filename
                part_bio.seek(0)
                await update.message.reply_document(document=part_bio, filename=filename)
        else:
            tmp_name = tmp.name
            tmp.close()
            with open(tmp_name, 'rb') as f:
                await update.message.reply_document(document=f, filename=f"{bin_num}_{qty}.txt")
    finally:
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    context.user_data.clear()
    await update.message.reply_text("✅ Done! Send `/gen <BIN>` to generate again.")
    return ConversationHandler.END


async def split_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Split a replied-to text document into multiple files of given lines: /split N"""
    # Expect one integer argument: number of lines per split
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: reply to a .txt file with: /split <lines_per_file>")
        return

    lines_per = int(context.args[0])
    if lines_per <= 0:
        await update.message.reply_text("Lines per split must be a positive integer")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text("Please reply to the .txt file you want to split with the /split command")
        return

    # Download the file into memory
    try:
        # Use the Document.get_file() async helper and download to memory
        file = await reply.document.get_file()
        bio = io.BytesIO()
        # download_to_memory is the async method that writes into the BytesIO
        await file.download_to_memory(out=bio)
        bio.seek(0)
        content = bio.read().decode(errors='replace')
    except Exception as e:
        await update.message.reply_text(f"Failed to download file: {e}")
        return

    lines = content.splitlines()
    total = len(lines)
    if total == 0:
        await update.message.reply_text("The file appears to be empty")
        return

    parts = (total + lines_per - 1) // lines_per
    base_name = reply.document.file_name or "split_file.txt"
    base_root = base_name.rsplit('.', 1)[0]

    for idx in range(parts):
        start = idx * lines_per
        part_lines = lines[start:start + lines_per]
        part_bio = io.BytesIO("\n".join(part_lines).encode())
        part_index = idx + 1
        # filename format: original_split_{part}_of_{total}.txt
        filename = f"{base_root}_split_{part_index}_of_{parts}.txt"
        part_bio.name = filename
        part_bio.seek(0)
        await update.message.reply_document(document=part_bio, filename=filename)

    await update.message.reply_text(f"Split done: {parts} file(s) created (total {total} lines)")


async def filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filter duplicates from a replied-to text document and return cleaned.txt"""
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text("Please reply to the .txt file you want to filter with: /filter")
        return

    try:
        file = await reply.document.get_file()
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)
        content = bio.read().decode(errors='replace')
    except Exception as e:
        await update.message.reply_text(f"Failed to download file: {e}")
        return

    lines = content.splitlines()
    seen = set()
    unique_lines = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)

    out_bio = io.BytesIO("\n".join(unique_lines).encode())
    out_bio.name = "cleaned.txt"
    out_bio.seek(0)
    await update.message.reply_document(document=out_bio, filename=out_bio.name)
    await update.message.reply_text(f"Filtered duplicates: {len(lines)} -> {len(unique_lines)} lines. Returned cleaned.txt")

    # Clear temporary user data and finish conversation
    context.user_data.clear()
    await update.message.reply_text("✅ Done!")
    return ConversationHandler.END


def main():
    if not TOKEN:
        print("ERROR: TG_TOKEN environment variable not set and no token provided.")
        return

    app = Application.builder().token(TOKEN).build()
    
    # Conversation handler for step-by-step input
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("gen", gen_command)],
        states={
            ASK_CARD_DETAILS: [MessageHandler(filters.TEXT & (~filters.COMMAND), ask_card_details)],
            ASK_QUANTITY: [MessageHandler(filters.TEXT & (~filters.COMMAND), ask_quantity)],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("split", split_command))
    app.add_handler(CommandHandler("filter", filter_command))

    print("Bot starting (Ctrl-C to stop)...")
    app.run_polling()


if __name__ == '__main__':
    main()