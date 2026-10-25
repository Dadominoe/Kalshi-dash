"""The sign-in page, the owner's admin page and the little 'signed in as' bar for a hosted copy. Plain HTML; display only."""
import html
import os
import time

MEMBERSHIP_ON = os.environ.get("MEMBERSHIP_ON", "") == "1"        # paid memberships on (see membership.py); off = the site behaves exactly as before

_CSS = ("body{margin:0;background:#0f1419;color:#e6edf3;font-family:Segoe UI,Arial,sans-serif;font-size:17px}"
        ".box{max-width:420px;margin:8vh auto;padding:26px;background:#161b22;border:1px solid #30363d;border-radius:12px}"
        ".wide{max-width:760px}h1{font-size:24px;margin:0 0 16px}h2{font-size:19px;margin:26px 0 8px}"
        "label{display:block;margin:12px 0 4px;color:#9da7b3;font-size:15px}"
        "input{width:100%;box-sizing:border-box;padding:11px;font-size:17px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px}"
        "button{margin-top:16px;padding:11px 18px;font-size:17px;background:#238636;color:#fff;border:0;border-radius:8px;cursor:pointer}"
        "button.red{background:#b62324;margin:0;padding:6px 12px;font-size:14px}button.grey{background:#30363d;margin:0;padding:6px 12px;font-size:14px}"
        ".msg{padding:10px 12px;border-radius:8px;background:#1f2a1f;border:1px solid #2ea043;margin-bottom:12px;font-size:15px}.err{background:#2a1f1f;border-color:#f85149}"
        "table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid #30363d;text-align:left;font-size:15px;vertical-align:middle}"
        ".on{color:#3fb950}.off{color:#8b949e}a{color:#58a6ff}.row{display:flex;gap:8px;align-items:center}.row input{width:auto;flex:1;padding:6px;font-size:14px}")


def _page(title, body, wide=False):
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><div class="box{" wide" if wide else ""}">{body}</div></body></html>')


SHORT_NOTICE = "For information and education only. Not gambling, not betting, and not financial, investment or trading advice. Nothing here places a trade or accepts a wager. Calls and percentages come from a statistical model and can be wrong. Trading event contracts can lose all the money you put in. Not affiliated with or endorsed by any exchange or data provider."
TERMS_DATE = "October 19, 2026"


def legal_footer():
    """Small grey notice for the bottom of every page, with a link to the full terms."""
    return ('<div style="max-width:900px;margin:26px auto 14px;padding:0 14px;font:12px/1.5 Segoe UI,Arial,sans-serif;color:#8b949e;text-align:center">'
            + html.escape(SHORT_NOTICE) + ' <a href="/terms" style="color:#58a6ff">Full terms and disclaimers</a></div>')


def terms_page():
    sections = [
        ("What this is", "The Dominator is an information and education tool. It shows prices, charts, order-flow readings and statistical model outputs (including the 'END CALL', percentages, streaks and track records) about short-term crypto prediction markets. The same general information is published to everyone who has access. It is not based on, and is not tailored to, any person's accounts, money, goals or circumstances."),
        ("Not gambling", "This is not a gambling or betting service. It does not take bets, wagers or deposits, does not pay out winnings, and does not place, route or execute any trade for anyone. Nothing on it is an invitation or instruction to bet or trade."),
        ("Not advice, and no personal advice", "Nothing here is financial, investment, trading, legal or tax advice, a recommendation, or an offer or solicitation of any kind. The owner does not hold themselves out as a registered investment adviser, broker, dealer or commodity trading advisor, and does not give personal trading advice. Please do not ask the owner or other members what you personally should do with your money; any message, chat or comment from anyone is only a personal opinion, not advice. If you want advice, speak to a licensed professional who knows your situation."),
        ("Calls can be wrong", "Every call, percentage, flip meter and chart reading is produced by a model from past data. Models are often wrong. Data can be late, missing or incorrect, and the site can go down. The win and loss records and streaks shown are a record of the tool's own calls. They are hypothetical, not the results of any real trades, and they ignore fees, slippage, fills and delays. Hypothetical results have many limits and do not predict future results. Past performance does not guarantee future results. No profit, win rate or result is promised or implied."),
        ("Risk", "Trading event contracts and other markets involves a high level of risk and you can lose all of the money you put in. Only use money you can afford to lose. Whatever you decide to do, and whether it is allowed where you live, is your own responsibility and your own decision."),
        ("Who may use it", "You must be of legal age where you live (at least 18, or older if your local law says so), and you must make sure using this information and any related markets is legal where you are. Do not use this site if it is not."),
        ("Fees", "Access is a flat rate of $300, paid to the owner directly. The fee is only for access to the information. It is never a share of, a payment for, or a promise about any trading result."),
        ("Independent tool", "The Dominator is an independent tool. It is not affiliated with, sponsored by or endorsed by any exchange, market or data provider. Data from outside sources can differ from what you see here."),
        ("As is, and limits on responsibility", "Everything is provided as is, with no promise that it is accurate, complete, on time or available. To the fullest extent the law allows, the owner is not responsible for any loss or damage, direct or indirect, from using, or being unable to use, this site or relying on anything on it. You agree to cover the owner for claims that come from your own use of the information or your own trading. If any part of these terms is not allowed where you live, the rest still applies."),
        ("Your access", "Access is by approval and can be changed or removed at any time, without notice. Do not share your login. Do not copy, redistribute or republish the content without the owner's written permission. The owner may change these terms; using the site after a change means you accept it."),
        ("Your information", "We keep the user name you choose, a scrambled (hashed) version of your password, and when you agreed to these terms. Your chart choices are kept in your own browser. We do not share your information with anyone. We never ask for, and you should never type here, any exchange keys, bank or card details."),
    ]
    if MEMBERSHIP_ON:
        sections = [(t, x) for t, x in sections if t not in ("Fees", "Your access", "Your information")] + [
            ("Memberships and fees", "Access is by paid membership: 10 days for $65, 30 days for $165, or 3.5 months for $379. Every new account gets one free 3-hour pass (one per person). Each plan renews automatically at the same price when its time is up, until you cancel; you keep what you already paid for after you cancel. Payments are handled by Stripe, so we never see or store your card details. Members who paid the original flat $300 are lifetime members: they keep access for life and never pay again. The fee is only for access to the information. It is never a share of, a payment for, or a promise about any trading result."),
            ("Your access", "Access lasts for the time you have paid for and can be changed or removed at any time. Do not share your login. Do not copy, redistribute or republish the content without the owner's written permission. The owner may change these terms; using the site after a change means you accept it."),
            ("Your information", "We keep the user name you choose, your e-mail address, a scrambled (hashed) version of your password, when you agreed to these terms, and when your membership starts and ends. Your chart choices are kept in your own browser. Card payments are taken by Stripe, not by us. We do not sell or share your information. We never ask for, and you should never type here, any exchange keys or bank details."),
        ]
    body = "".join(f"<h2>{html.escape(t)}</h2><p>{html.escape(x)}</p>" for t, x in sections)
    body = (f'<h1>Terms and disclaimers</h1><p style="color:#8b949e;font-size:14px">Last updated {TERMS_DATE}.</p>{body}'
            '<p style="color:#8b949e;font-size:14px;margin-top:24px">By using this site you agree to these terms. If you do not agree, do not use it.</p>'
            '<p><a href="javascript:history.back()">Back</a></p>')
    return _page("Terms and disclaimers", body, wide=True)


def _msg(m, err=False):
    return f'<div class="msg{" err" if err else ""}">{html.escape(m)}</div>' if m else ""


def _tiny():
    return '<p style="margin-top:16px;font-size:12px;line-height:1.5;color:#8b949e">' + html.escape(SHORT_NOTICE) + ' <a href="/terms">Full terms</a></p>'


def login_page(msg="", err=True):
    if MEMBERSHIP_ON:
        try:
            from src import membership as _m
            plans = _m.plans()["plans"]
        except Exception:
            plans = []
        cards = "".join(
            f'<div style="flex:1 1 140px;border:1px solid {"#d29922" if p.get("best") else "#30363d"};border-radius:10px;padding:14px;background:#0d1117;text-align:center">'
            f'{"<div style=color:#d29922;font-size:12px;font-weight:700>BEST VALUE</div>" if p.get("best") else ""}'
            f'<div style="font-size:13px;color:#9da7b3">{html.escape(p["name"].upper())}</div><div style="font-size:30px;font-weight:700">${p["price"]}</div>'
            f'<div style="font-size:12px;color:#8b949e">${p["price"] / p["days"]:.2f} a day</div></div>' for p in plans)
        return _page("The Dominator", '<div style="text-align:center"><div style="font-size:13px;letter-spacing:3px;color:#8b949e">LIVE MARKET INTELLIGENCE</div>'
                     '<h1 style="font-size:34px;margin:6px 0 4px">THE DOMINATOR</h1><div style="color:#9da7b3;margin-bottom:18px">Live calls, flip warnings, order flow and charts for the 15-minute BTC and XRP rounds.</div></div>'
                     f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px">{cards}</div>'
                     '<div class="msg" style="text-align:center">Try it free: <b>one 3-hour pass</b> for every new account. Plans renew automatically until you cancel.</div>'
                     '<a href="/signup"><button type="button" style="width:100%;margin-top:4px;font-size:19px">Create an account</button></a>'
                     f'<h2 style="margin-top:26px">Already a member? Sign in</h2>{_msg(msg, err)}'
                     '<form method="post" action="/login"><label>User name</label><input name="u" autocomplete="username" required>'
                     '<label>Password</label><input name="p" type="password" autocomplete="current-password" required>'
                     '<button type="submit">Sign in</button></form>'
                     '<p style="margin-top:14px;font-size:14px;color:#9da7b3;text-align:center">Original <b>$300 members are locked in for life</b> - nothing more to pay. Just sign in.</p>' + _tiny(), wide=True)
    return _page("Sign in", f'<h1>The Dominator Live Dashboard</h1>{_msg(msg, err)}'
                 '<form method="post" action="/login"><label>User name</label><input name="u" autocomplete="username" autofocus required>'
                 '<label>Password</label><input name="p" type="password" autocomplete="current-password" required>'
                 '<button type="submit">Sign in</button></form>'
                 '<p style="margin-top:18px;font-size:15px">No account yet? Access is a <b>$300 flat rate</b>. <a href="/signup">Ask for access</a></p>' + _tiny())


def signup_page(msg="", err=True, done=False):
    if MEMBERSHIP_ON:
        return _page("Create an account", f'<h1>Create an account</h1>{_msg(msg, err)}<div class="msg" style="font-size:16px">Free: one <b>3-hour pass</b> per person to try it. Then plans from <b>$65</b>. Payment is handled by Stripe.</div>'
                     '<form method="post" action="/signup"><label>Pick a user name (letters, numbers, dot, dash, underscore)</label><input name="u" autocomplete="username" required>'
                     '<label>Your e-mail address</label><input name="e" type="email" autocomplete="email" required>'
                     '<label>Pick a password (at least 8 characters)</label><input name="p" type="password" autocomplete="new-password" minlength="8" required>'
                     '<label>Your Discord name (optional)</label><input name="n" maxlength="100" placeholder="for example  nate_trades">'
                     '<label style="display:flex;gap:10px;align-items:flex-start;margin-top:16px;color:#e6edf3"><input type="checkbox" name="agree" value="1" required style="width:auto;margin-top:4px"><span>I am of legal age where I live, I understand this is for information only and not gambling or advice, and I agree to the <a href="/terms" target="_blank">Terms and disclaimers</a>.</span></label>'
                     '<button type="submit">Create my account</button></form><p style="margin-top:18px;font-size:15px"><a href="/login">Back to sign in</a></p>')
    if done:
        return _page("Request sent", '<h1>The Dominator Live Dashboard</h1>' + _msg(msg, False) + '<p>Access is a $300 flat rate. Your account is waiting for the owner to approve it. Once access has been arranged with the owner and approved, you can sign in here with the user name and password you just chose.</p><p><a href="/login">Back to sign in</a></p>')
    return _page("Ask for access", f'<h1>Ask for access</h1>{_msg(msg, err)}<div class="msg" style="font-size:17px"><b>Price: $300 flat rate.</b> You pay the owner directly, and your account is approved once it is arranged. The price is for access to the information only.</div>'
                 '<form method="post" action="/signup"><label>Pick a user name (letters, numbers, dot, dash, underscore)</label><input name="u" autocomplete="username" required>'
                 '<label>Pick a password (at least 8 characters)</label><input name="p" type="password" autocomplete="new-password" minlength="8" required>'
                 '<label>Your Discord name (so the owner knows who you are)</label><input name="n" maxlength="100" placeholder="for example  nate_trades" required>'
                 '<label style="display:flex;gap:10px;align-items:flex-start;margin-top:16px;color:#e6edf3"><input type="checkbox" name="agree" value="1" required style="width:auto;margin-top:4px"><span>I am of legal age where I live, I understand this is for information only and not gambling or advice, and I agree to the <a href="/terms" target="_blank">Terms and disclaimers</a>.</span></label><button type="submit">Send request</button></form><p style="margin-top:18px;font-size:15px"><a href="/login">Back to sign in</a></p>')


def _member_line(u):
    if u.get("lifetime"):
        return '<br><span class="on">LIFETIME member ($300) - never expires</span>'
    if "member_until" not in u:
        return '<br><span class="off">no end date</span>' if MEMBERSHIP_ON else ""
    until = float(u.get("member_until") or 0)
    left = "ended" if until <= time.time() else "until " + time.strftime("%b %d %H:%M UTC", time.gmtime(until))
    return f'<br><span class="off">{html.escape(u.get("plan") or "no plan")} - {left}{" - " + html.escape(u["email"]) if u.get("email") else ""}</span>'


def admin_page(users, online, msg="", err=False, requests=None, warn=""):
    rows = []
    for name in sorted(users):
        seen = online.get(name)
        if seen and time.time() - seen < 90:
            st = '<span class="on">online now</span>'
        elif seen:
            st = f'<span class="off">{int((time.time() - seen) // 60)} min ago</span>'
        else:
            st = '<span class="off">not since the site restarted</span>'
        n = html.escape(name)
        rows.append(f'<tr><td><b>{n}</b><br><span class="off">Discord: {html.escape(users[name].get("note") or "-")}</span>{_member_line(users[name])}</td><td>{st}</td>'
                    f'<td><form method="post" action="/admin/passwd" class="row"><input type="hidden" name="u" value="{n}">'
                    f'<input name="p" type="password" placeholder="new password" minlength="8" required><button class="grey">Change password</button></form></td>'
                    f'<td><form method="post" action="/admin/lifetime" style="margin:0 0 6px"><input type="hidden" name="u" value="{n}"><input type="hidden" name="on" value="{0 if users[name].get("lifetime") else 1}"><button class="grey">{"Lifetime: ON (click to turn off)" if users[name].get("lifetime") else "Make lifetime"}</button></form>'
                    f'<td><form method="post" action="/admin/delete" onsubmit="return confirm(\'Remove {n}? They will be signed out right away.\')">'
                    f'<input type="hidden" name="u" value="{n}"><button class="red">Remove</button></form></td></tr>')
    table = ('<table><tr><th>Person</th><th>Last seen</th><th>Password</th><th></th></tr>' + "".join(rows) + '</table>') if rows else '<p>Nobody yet. Add the first person below.</p>'
    reqs = ""
    if requests:
        rr = []
        for name in sorted(requests, key=lambda k: requests[k].get("time", 0)):
            n = html.escape(name)
            who = html.escape(requests[name].get("note", ""))
            when = time.strftime("%b %d %H:%M UTC", time.gmtime(requests[name].get("time", 0)))
            rr.append(f'<tr><td><b>{n}</b></td><td>{who}<br><span class="off">{when}</span></td>'
                      f'<td><form method="post" action="/admin/approve" style="display:inline"><input type="hidden" name="u" value="{n}"><button class="grey" style="background:#238636">Approve</button></form> '
                      f'<form method="post" action="/admin/deny" style="display:inline"><input type="hidden" name="u" value="{n}"><button class="red">Deny</button></form></td></tr>')
        reqs = (f'<h2>Waiting for your approval ({len(requests)})</h2><table><tr><th>User name</th><th>Discord name</th><th></th></tr>' + "".join(rr) + '</table>')
    warn_html = (f'<div class="msg err" style="font-size:16px"><b>Warning:</b> {html.escape(warn)}</div>') if warn else ""
    backup = ('<h2>Your records</h2><p><a href="/admin/people.csv"><b>Download the people list</b></a> - everybody who ever asked: user name, Discord name, '
              'when they asked, when you approved them, the terms they agreed to, and anyone denied or removed. Opens in Excel or Google Sheets.</p>'
              '<p><a href="/admin/people_log.csv">Download the full history</a> - every ask, approve, deny, remove and password change, in order. Nothing is ever erased from it.</p>'
              '<h2>Backup</h2><p><a href="/admin/backup">Download a backup of everybody</a> (user names, scrambled passwords and the full history - keep it private). '
              'Restoring it puts back the people and the history.</p>'
              '<form method="post" action="/admin/restore"><label>Restore: paste the contents of a backup file here (it only adds people who are missing)</label>'
              '<input name="j" placeholder="paste the backup text here"><button class="grey" style="margin-top:10px">Restore</button></form>')
    body = (f'<h1>People who can sign in</h1>{warn_html}{_msg(msg, err)}<p><a href="/">Back to the dashboard</a> &nbsp; <a href="/logout">Sign out</a></p>{reqs}{table}'
            '<h2>Add a person</h2><form method="post" action="/admin/add"><label>User name (letters, numbers, dot, dash, underscore)</label><input name="u" required>'
            '<label>Their password (at least 8 characters - tell it to them yourself)</label><input name="p" type="password" minlength="8" required>'
            '<button type="submit">Add person</button></form>' + backup)
    return _page("People", body, wide=True)


def userbar(name, is_admin):
    link = '<a href="/admin" style="color:#58a6ff">People</a> &nbsp; ' if is_admin else ('<a href="/join" style="color:#58a6ff">Membership</a> &nbsp; ' if MEMBERSHIP_ON else "")
    return ('<div style="position:fixed;top:3px;right:8px;z-index:99999;font:12px Segoe UI,Arial,sans-serif;color:#9da7b3;background:rgba(15,20,25,.78);padding:2px 8px;border-radius:6px">'
            f'{html.escape(name)} &nbsp; {link}<a href="/logout" style="color:#58a6ff">Sign out</a></div>')


PEOPLE_COLUMNS = (("user_name", "User name"), ("discord_name", "Discord name"), ("status", "Status"), ("asked_on", "Asked on"),
                  ("approved_on", "Approved on"), ("terms_agreed", "Terms agreed to"), ("terms_agreed_on", "Agreed to terms on"),
                  ("last_seen", "Last seen"), ("denied_on", "Denied on"), ("removed_on", "Removed on"))
LOG_COLUMNS = (("time", "When"), ("event", "What happened"), ("name", "User name"), ("discord", "Discord name"),
               ("asked_at", "Asked on"), ("terms", "Terms"), ("agreed_at", "Agreed to terms on"))
_TIME_KEYS = {"asked_on", "approved_on", "terms_agreed_on", "last_seen", "denied_on", "removed_on", "time", "asked_at", "agreed_at", "approved_at"}


def _csv(rows, columns, fmt_time):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow([fmt_time(r.get(k)) if k in _TIME_KEYS else (r.get(k) or "") for k, _ in columns])
    return buf.getvalue()


def people_csv(rows, fmt_time):
    return _csv(rows, PEOPLE_COLUMNS, fmt_time)


def log_csv(entries, fmt_time):
    return _csv(entries, LOG_COLUMNS, fmt_time)
