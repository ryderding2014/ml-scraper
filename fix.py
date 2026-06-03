"""Fix: replace launch_persistent_context with launch + new_context"""
import re

with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace the _create_context function
old_func_start = 'async def _create_context(pw):\n    os.makedirs'
new_func = '''async def _create_context(pw):
    """Create a fresh non-persistent context each time (avoids ML bot detection)."""
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-setuid-sandbox", "--no-first-run",
            "--disable-default-apps", "--disable-infobars",
            "--disable-background-networking", "--disable-sync",
            "--disable-features=TranslateUI",
            "--disable-extensions",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        geolocation={"latitude": -23.5505, "longitude": -46.6333},
        permissions=["geolocation"],
        ignore_https_errors=True,
        extra_http_headers={
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    )
    await context.add_init_script(STEALTH_SCRIPT)
    return context, browser'''

# Find function start and end
start = content.find(old_func_start)
if start == -1:
    print('ERROR: function start not found')
else:
    # Find the end of the function (next 'async def' or 'def')
    end = start
    while end < len(content):
        end += 1
        if content[end:end+4] == 'def ' or content[end:end+6] == 'async ':
            break
    # Go back to before this decl
    while content[end-1] == '\n':
        end -= 1

    content = content[:start] + new_func + '\n\n' + content[end:]
    print(f'Replaced function from {start} to {end}')

# 2. Update all call sites from 'context = await _create_context(pw)' to 'context, browser = await _create_context(pw)'
content = content.replace('context = await _create_context(pw)', 'context, browser = await _create_context(pw)')

# 3. Update cleanup: 'await context.close()' -> 'await browser.close()'
# But don't change the finally block
# The main flow has 'await context.close()' which should be 'await browser.close()'
# Actually, context.close() still works, but browser.close() is cleaner
# Let's leave context.close() as-is since it just closes the context

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done')
