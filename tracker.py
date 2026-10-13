#!/usr/bin/env python3
"""
Tygodniowy tracker cen Pokemon TCG (Cardmarket) - wpisujesz cene recznie.

Dwie strefy:
  SEALED - produkty kupione (podajesz cene zakupu) -> wydane vs wartosc, zwrot
  KARTY  - np. trafione z boosterow (bez ceny zakupu) -> tylko przyrost ceny od startu sledzenia

Uzycie:
    python tracker.py            -> runda cen dla wszystkiego (sealed, potem karty)
    python tracker.py sealed     -> runda tylko dla sealed
    python tracker.py karty      -> runda tylko dla kart
    python tracker.py dodaj      -> dodaj produkt lub karte
    python tracker.py stala      -> ustaw/wylacz "stala cene" (pozycja pomijana w cotygodniowej rundzie)
    python tracker.py otworz     -> zapisz otwarcie produktu: ile kosztowalo + karty, ktore dropnely
    python tracker.py usun       -> usun produkt (z historia cen) albo pojedynczy wpis ceny
    python tracker.py historia   -> dopisz ceny z wczesniejszej daty (np. z zeszlego tygodnia)
    python tracker.py wykres     -> tylko odswiez wykres
    python tracker.py lista      -> pokaz wszystko i ostatnie ceny

Wymaga:  pip install plotly
Dane: folder "dane" obok skryptu (produkty.csv, ceny.csv). Edytuj w Notatniku, nie w Excelu.
"""

import csv
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ----------------------------- USTAWIENIA -----------------------------
# Prowizja przy sprzedazy sealed (w %). 0 = wartosc rynkowa bez prowizji.
FEE_PERCENT = 0.0

# Zmiana ceny (w %) ponizej tego progu w obie strony = "bez trendu".
TREND_THRESHOLD = 3.0

# Jesli nowa cena rozni sie od poprzedniej o wiecej niz tyle %, program zapyta "na pewno?".
CONFIRM_JUMP_PERCENT = 30.0

# Podswietlanie rosnacych kart: cena teraz >= MIN_PRICE (€) oraz wzrost >= MIN_GAIN (%)
# liczony od startu sledzenia LUB w ostatnich 30 dniach. Karty ze stala cena nie sa podswietlane.
HIGHLIGHT_MIN_PRICE = 10.0
HIGHLIGHT_MIN_GAIN = 20.0

# Kurs do przeliczania kosztu otwarc wpisanego w zlotowkach (zl na 1 €).
# USTAW AKTUALNY KURS - 4.25 to tylko wartosc przykladowa.
PLN_PER_EUR = 4.38
# ----------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
DATA = BASE / "dane"
PRODUCTS_FILE = DATA / "produkty.csv"
PRICES_FILE = DATA / "ceny.csv"
CHART_FILE = BASE / "wykres.html"
OPENINGS_FILE = DATA / "otwarcia.csv"

PRODUCT_FIELDS = ["id", "typ", "nazwa", "link", "ilosc", "cena_zakupu", "data_zakupu", "stala", "otwarcie"]
PRICE_FIELDS = ["data", "id", "cena"]
OPENING_FIELDS = ["id", "data", "nazwa", "koszt", "wartosc_dropu"]
SEALED, CARD = "sealed", "karta"


# ----------------------------- POMOCNICZE -----------------------------
def parse_money(text):
    """'147,50', '147.5', '1.234,56', '147 €' -> float"""
    s = text.strip().replace("€", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    value = float(s)
    if value < 0:
        raise ValueError("ujemna kwota")
    return value


def parse_cost(text):
    """'200 zl' / '200zł' / '200 pln' -> € wg PLN_PER_EUR; bez przyrostka = €"""
    t = text.strip().lower()
    for suffix in ("zł", "zl", "pln"):
        if t.endswith(suffix):
            
            return parse_money(t[: -len(suffix)]) / PLN_PER_EUR
    return parse_money(t)


def parse_date(text):
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def fmt_eur(x: float):
    return f"{x:,.2f} €".replace(",", " ")



def fmt_pct(x):
    return "–" if x is None else f"{x:+.1f}%"


def with_english_filter(url):
    """Dodaje language=1 (angielski) do linku; inne parametry zostaja."""
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query["language"] = "1"
    return urlunparse(parts._replace(query=urlencode(query)))


def ensure_files():
    DATA.mkdir(exist_ok=True)
    if not PRODUCTS_FILE.exists():
        with open(PRODUCTS_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, PRODUCT_FIELDS).writeheader()
    if not PRICES_FILE.exists():
        with open(PRICES_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, PRICE_FIELDS).writeheader()
    if not OPENINGS_FILE.exists():
        with open(OPENINGS_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, OPENING_FIELDS).writeheader()


def price_at(points, day):
    """Ostatnia znana cena z dnia <= day (points posortowane)."""
    value = None
    for d, p in points:
        if d <= day:
            value = p
        else:
            break
    return value


def pct_change(new, old):
    if old is None or old == 0:
        return None
    return (new - old) / old * 100


def changes(rec):
    """rec = posortowana lista (data, cena) -> (teraz, zmiana 7d %, zmiana 30d %)"""
    now = rec[-1][1]
    if len(rec) < 2:
        return now, None, None
    last_day = rec[-1][0]
    ch7 = pct_change(now, price_at(rec, last_day - timedelta(days=7)))
    ch30 = pct_change(now, price_at(rec, last_day - timedelta(days=30)))
    return now, ch7, ch30


def trend_label(ch7, ch30):
    basis = ch30 if ch30 is not None else ch7
    if basis is None:
        return "za mało danych"
    if basis > TREND_THRESHOLD:
        return "▲ rośnie"
    if basis < -TREND_THRESHOLD:
        return "▼ spada"
    return "● bez trendu"


# ----------------------------- ODCZYT / ZAPIS -----------------------------
def read_products():
    ensure_files()
    products = []
    with open(PRODUCTS_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            cena = row.get("cena_zakupu")
            products.append(
                {
                    "id": int(row["id"]),
                    "typ": (row.get("typ") or SEALED).strip().lower(),  # stare pliki = sealed
                    "nazwa": row["nazwa"],
                    "link": row["link"],
                    "ilosc": int(row["ilosc"]),
                    "cena_zakupu": float(cena) if cena not in (None, "") else None,
                    "data_zakupu": parse_date(row["data_zakupu"]),
                    "stala": (row.get("stala") or "").strip().lower() in ("1", "tak", "t", "true"),
                    "otwarcie": int(row["otwarcie"]) if (row.get("otwarcie") or "").strip() else None,
                }
            )
    return products


def write_products(products):
    with open(PRODUCTS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, PRODUCT_FIELDS)
        w.writeheader()
        for p in products:
            w.writerow(
                {
                    **p,
                    "cena_zakupu": "" if p["cena_zakupu"] is None else p["cena_zakupu"],
                    "data_zakupu": p["data_zakupu"].isoformat(),
                    "stala": "1" if p.get("stala") else "",
                    "otwarcie": "" if p.get("otwarcie") is None else p["otwarcie"],
                }
            )


def read_openings():
    ensure_files()
    result = []
    with open(OPENINGS_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            result.append({"id": int(row["id"]), "data": parse_date(row["data"]), "nazwa": row["nazwa"],
                           "koszt": float(row["koszt"]), "wartosc_dropu": float(row["wartosc_dropu"])})
    return result


def write_openings(openings):
    with open(OPENINGS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, OPENING_FIELDS)
        w.writeheader()
        for o in openings:
            w.writerow({"id": o["id"], "data": o["data"].isoformat(), "nazwa": o["nazwa"],
                        "koszt": f"{o['koszt']:.2f}", "wartosc_dropu": f"{o['wartosc_dropu']:.2f}"})


def read_prices():
    """-> dict: id produktu -> posortowana lista (data, cena)"""
    ensure_files()
    prices = {}
    with open(PRICES_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            prices.setdefault(int(row["id"]), []).append(
                (parse_date(row["data"]), float(row["cena"]))
            )
    for lst in prices.values():
        lst.sort()
    return prices


def save_price(day, product_id, price):
    """Zapisuje cene; wpis z tego samego dnia dla tego samego produktu jest nadpisywany."""
    prices = read_prices()
    lst = [(d, p) for d, p in prices.get(product_id, []) if d != day]
    lst.append((day, price))
    prices[product_id] = sorted(lst)
    write_prices(prices)


def write_prices(prices):
    with open(PRICES_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, PRICE_FIELDS)
        w.writeheader()
        for pid, items in sorted(prices.items()):
            for d, p in items:
                w.writerow({"data": d.isoformat(), "id": pid, "cena": f"{p:.2f}"})


# ----------------------------- WPISYWANIE CEN -----------------------------
def ask_and_save_price(product, last, today):
    """Pyta o cene jednego produktu. Zwraca True, jesli uzytkownik wpisal 'q'."""
    while True:
        raw = input("   Cena (€): ").strip()
        if raw == "":
            print("   pominieto\n")
            return False
        if raw.lower() == "q":
            return True
        try:
            price = parse_money(raw)
        except ValueError:
            print("   Nie rozumiem tej kwoty, sprobuj np. 147,50")
            continue
        if last and last[-1][1] > 0:
            jump = abs(price - last[-1][1]) / last[-1][1] * 100
            if jump > CONFIRM_JUMP_PERCENT:
                ok = input(f"   To zmiana o {jump:.0f}% wzgledem poprzedniej. Na pewno? (t/n): ")
                if ok.strip().lower() not in ("t", "tak", "y", "yes"):
                    continue
        save_price(today, product["id"], price)
        print(f"   zapisano {fmt_eur(price)}\n")
        return False


# ----------------------------- KOMENDY -----------------------------
def cmd_add():
    products = read_products()
    print("Dodawanie (Ctrl+C aby anulowac)\n")
    print("Typ:  1 = sealed (kupiony, podajesz cene zakupu)")
    print("      2 = karta  (bez ceny zakupu, liczy sie tylko przyrost ceny)")
    kind = input("Wybierz 1 lub 2: ").strip()
    if kind not in ("1", "2"):
        print("Anulowano.")
        return

    name = input("Nazwa: ").strip()
    url = input("Link do Cardmarket: ").strip()
    if "cardmarket.com" not in url:
        print("To nie wyglada na link do Cardmarket. Anulowano.")
        return

    fixed = False
    try:
        if kind == "1":
            qty = int(input("Ilosc sztuk: ").strip())
            price = parse_money(input("Cena zakupu za 1 sztuke (€): "))
            raw = input("Data zakupu RRRR-MM-DD (Enter = dzis): ").strip()
            buy_date = parse_date(raw) if raw else date.today()
        else:
            raw = input("Ilosc sztuk (Enter = 1): ").strip()
            qty = int(raw) if raw else 1
            price = None
            buy_date = date.today()
            fixed = confirm("Stala cena? (t = nie pytaj o nia co tydzien, np. tania karta)")
    except ValueError:
        print("Niepoprawna wartosc. Anulowano.")
        return
    if qty < 1:
        print("Ilosc musi byc >= 1. Anulowano.")
        return

    new_id = max((p["id"] for p in products), default=0) + 1
    new = {
        "id": new_id,
        "typ": SEALED if kind == "1" else CARD,
        "nazwa": name,
        "link": url,
        "ilosc": qty,
        "cena_zakupu": price,
        "data_zakupu": buy_date,
        "stala": kind == "2" and fixed,
        "otwarcie": None,
    }
    products.append(new)
    write_products(products)
    print(f"Dodano: {name} (id {new_id}).")

    if kind == "2":
        if fixed:
            print("\nPodaj cene karty - bedzie traktowana jako stala (mozesz ja potem zmienic: python tracker.py stala).")
        else:
            print("\nPodaj aktualna cene karty - to bedzie punkt startowy, od ktorego liczony jest przyrost.")
        webbrowser.open(with_english_filter(url))
        ask_and_save_price(new, [], date.today())


def cmd_list():
    products = read_products()
    prices = read_prices()
    if not products:
        print("Brak produktow. Dodaj pierwszy: python tracker.py dodaj")
        return
    for p in products:
        last = prices.get(p["id"], [])
        last_txt = f"{fmt_eur(last[-1][1])} ({last[-1][0]})" if last else "brak wpisow"
        if p["typ"] == CARD:
            start_txt = f"start {fmt_eur(last[0][1])}" if last else "brak punktu startowego"
            fx = " | STALA CENA" if p.get("stala") else ""
            print(f"[{p['id']}] KARTA  {p['nazwa']} | {p['ilosc']} szt. | {start_txt} | ostatnia: {last_txt}{fx}")
        else:
            print(
                f"[{p['id']}] SEALED {p['nazwa']} | {p['ilosc']} szt. po {fmt_eur(p['cena_zakupu'])} "
                f"({p['data_zakupu']}) | ostatnia: {last_txt}" + (" | STALA CENA" if p.get("stala") else "")
            )
    ops = read_openings()
    if ops:
        c = sum(o["koszt"] for o in ops)
        v = sum(o["wartosc_dropu"] for o in ops)
        print(f"OTWARCIA: {len(ops)} | wydane {fmt_eur(c)} | drop {fmt_eur(v)} | wynik {v - c:+.2f} €")


def cmd_weekly(only=None):
    products = read_products()
    if only:
        products = [p for p in products if p["typ"] == only]
    if not products:
        print("Brak produktow do wyceny. Dodaj: python tracker.py dodaj")
        return
    # sealed najpierw, potem karty
    products.sort(key=lambda p: (p["typ"] == CARD, p["id"]))
    prices = read_prices()
    today = date.today()
    # pozycje ze stala cena (i z wpisana juz jakas cena) nie sa wyceniane co tydzien
    skipped = [p for p in products if p.get("stala") and prices.get(p["id"])]
    products = [p for p in products if p not in skipped]
    print(f"Tygodniowa runda cen - {today}")
    print("Dla kazdego produktu otworze strone. Wpisz srednia z ok. 5 najtanszych ofert")
    print("angielskich (dla kart: cene wlasciwej wersji/stanu). Enter = pomin, q = zakoncz.")
    if skipped:
        print(f"Pomijam {len(skipped)} poz. ze stala cena: " + ", ".join(p["nazwa"] for p in skipped))
    print()
    if not products:
        print("Wszystkie pozycje maja stala cene - nie ma o co pytac.\n")
        build_chart()
        return

    group = None
    for i, p in enumerate(products, 1):
        if p["typ"] != group:
            group = p["typ"]
            print("=== SEALED ===" if group == SEALED else "=== KARTY ===")
        last = prices.get(p["id"], [])
        last_txt = f"poprzednio {fmt_eur(last[-1][1])} ({last[-1][0]})" if last else "brak poprzednich wpisow"
        if p["typ"] == CARD:
            start_txt = f"start {fmt_eur(last[0][1])}" if last else "bez punktu startowego"
            head = f"{p['nazwa']} - {start_txt}, {last_txt}"
        else:
            head = f"{p['nazwa']} - kupione po {fmt_eur(p['cena_zakupu'])}, {last_txt}"
        print(f"[{i}/{len(products)}] {head}")
        webbrowser.open(with_english_filter(p["link"]))
        if ask_and_save_price(p, last, today):
            print()
            break

    build_chart()


def cmd_open():
    products = read_products()
    openings = read_openings()
    prices = read_prices()
    print("Zapis otwarcia (Ctrl+C w trakcie dodawania kart zapisze to, co juz wpisales)\n")
    name = input("Co otworzyles (nazwa produktu): ").strip()
    if not name:
        print("Anulowano.")
        return
    try:
        cost = parse_cost(input(f"Ile zaplaciles lacznie? np. 200 zl albo 47,50 €: "))
        raw = input("Data RRRR-MM-DD (Enter = dzis): ").strip()
        day = parse_date(raw) if raw else date.today()
    except ValueError:
        print("Niepoprawna wartosc. Anulowano.")
        return

    op_id = max((o["id"] for o in openings), default=0) + 1
    print(type(cost))
    print(f"\nKoszt: {fmt_eur(cost)}. Teraz karty z dropu (sama nazwa + Enter = koniec).")
    print("Wartosc dropu zapisuje sie raz, wg dzisiejszych cen, i pozniej sie nie zmienia.")
    print("Jesli wpiszesz nazwe istniejacej karty ze stala cena (np. pula tanich kart), uzyje jej linku i ceny.\n")
    drop_total = 0.0
    try:
        while True:
            cname = input("Karta: ").strip()
            if not cname:
                break
            same = next((p for p in reversed(products)
                         if p["typ"] == CARD and p.get("stala") and p["nazwa"].lower() == cname.lower()), None)
            try:
                raw = input("   Ilosc (Enter = 1): ").strip()
                qty = int(raw) if raw else 1
                if qty < 1:
                    raise ValueError
                if same:
                    last = prices.get(same["id"], [])
                    price = last[-1][1] if last else parse_money(input("   Cena za 1 szt. (€): "))
                    link, fixed = same["link"], True
                else:
                    link = input("   Link do Cardmarket: ").strip()
                    if "cardmarket.com" not in link:
                        print("   To nie wyglada na link do Cardmarket, pomijam te karte.\n")
                        continue
                    price = parse_money(input("   Cena za 1 szt. (€): "))
                    fixed = confirm("   Stala cena?")
            except ValueError:
                print("   Niepoprawna wartosc, pomijam te karte.\n")
                continue
            new_id = max((p["id"] for p in products), default=0) + 1
            products.append({"id": new_id, "typ": CARD, "nazwa": cname if not same else same["nazwa"],
                             "link": link, "ilosc": qty, "cena_zakupu": None, "data_zakupu": day,
                             "stala": fixed, "otwarcie": op_id})
            write_products(products)
            save_price(day, new_id, price)
            prices = read_prices()
            drop_total += qty * price
            print(f"   dodano: {qty} x {fmt_eur(price)} = {fmt_eur(qty * price)}\n")
    except (KeyboardInterrupt, EOFError):
        print("\nPrzerwano - zapisuje otwarcie z kartami dodanymi do tej pory.")

    openings.append({"id": op_id, "data": day, "nazwa": name, "koszt": cost, "wartosc_dropu": drop_total})
    write_openings(openings)
    print(f"\nZapisano otwarcie: {name} | koszt {fmt_eur(cost)} | drop {fmt_eur(drop_total)} | "
          f"wynik {drop_total - cost:+.2f} € ({fmt_pct(pct_change(drop_total, cost))})\n")
    build_chart()


def confirm(question):
    return input(question + " (t/n): ").strip().lower() in ("t", "tak", "y", "yes")


def cmd_delete():
    products = read_products()
    if not products:
        print("Brak produktow.")
        return
    prices = read_prices()
    print("Co usunac?")
    print("  1 = caly produkt (razem z cala historia cen)")
    print("  2 = pojedynczy wpis ceny (np. blednie wpisana cena)")
    print("  3 = zapisane otwarcie (karty z dropu zostaja w kolekcji)")
    kind = input("Wybierz 1, 2 lub 3: ").strip()
    if kind not in ("1", "2", "3"):
        print("Anulowano.")
        return

    if kind == "3":
        openings = read_openings()
        if not openings:
            print("Brak zapisanych otwarc.")
            return
        for o in openings:
            print(f"  [{o['id']}] {o['data']} {o['nazwa']} - koszt {fmt_eur(o['koszt'])}, drop {fmt_eur(o['wartosc_dropu'])}")
        try:
            oid = int(input("\nNumer otwarcia do usuniecia: ").strip())
        except ValueError:
            print("Niepoprawny numer. Anulowano.")
            return
        if oid not in [o["id"] for o in openings]:
            print("Nie ma otwarcia o takim numerze. Anulowano.")
            return
        if not confirm("Usunac to otwarcie z wykresu? Karty zostaja w kolekcji."):
            print("Anulowano.")
            return
        write_openings([o for o in openings if o["id"] != oid])
        print("Usunieto.")
        if products:
            build_chart()
        return

    print()
    for p in products:
        tag = "KARTA " if p["typ"] == CARD else "SEALED"
        print(f"  [{p['id']}] {tag} {p['nazwa']} ({len(prices.get(p['id'], []))} wpisow cen)")
    try:
        pid = int(input("\nPodaj numer (id) produktu: ").strip())
    except ValueError:
        print("Niepoprawny numer. Anulowano.")
        return
    product = next((p for p in products if p["id"] == pid), None)
    if product is None:
        print("Nie ma produktu o takim numerze. Anulowano.")
        return

    if kind == "1":
        n = len(prices.get(pid, []))
        if not confirm(f"Usunac '{product['nazwa']}' i {n} wpisow cen? Tego nie da sie cofnac."):
            print("Anulowano.")
            return
        write_products([p for p in products if p["id"] != pid])
        prices.pop(pid, None)
        write_prices(prices)
        print("Usunieto.")
    else:
        entries = prices.get(pid, [])
        if not entries:
            print("Ten produkt nie ma wpisow cen.")
            return
        print(f"\nWpisy cen: {product['nazwa']}")
        for i, (d, pr) in enumerate(entries, 1):
            print(f"  {i}. {d}  {fmt_eur(pr)}")
        try:
            num = int(input("\nNumer wpisu do usuniecia: ").strip())
            if num < 1:
                raise IndexError
            d, pr = entries[num - 1]
        except (ValueError, IndexError):
            print("Niepoprawny numer. Anulowano.")
            return
        if not confirm(f"Usunac wpis {d} - {fmt_eur(pr)}?"):
            print("Anulowano.")
            return
        prices[pid] = [e for i, e in enumerate(entries, 1) if i != num]
        write_prices(prices)
        print("Usunieto.")
    if read_products():
        build_chart()


def cmd_fixed():
    products = read_products()
    if not products:
        print("Brak produktow.")
        return
    prices = read_prices()
    for p in products:
        tag = "KARTA " if p["typ"] == CARD else "SEALED"
        last = prices.get(p["id"], [])
        last_txt = fmt_eur(last[-1][1]) if last else "brak ceny"
        fx = "  [STALA CENA]" if p.get("stala") else ""
        print(f"  [{p['id']}] {tag} {p['nazwa']} - {last_txt}{fx}")
    try:
        pid = int(input("\nPodaj numer (id) produktu: ").strip())
    except ValueError:
        print("Niepoprawny numer. Anulowano.")
        return
    p = next((x for x in products if x["id"] == pid), None)
    if p is None:
        print("Nie ma produktu o takim numerze. Anulowano.")
        return
    last = prices.get(pid, [])
    last_txt = fmt_eur(last[-1][1]) if last else "brak"

    if p.get("stala"):
        raw = input(f"Stala cena obecnie: {last_txt}. Nowa cena (€), Enter = bez zmian, w = wylacz stala cene: ").strip()
        if raw.lower() == "w":
            p["stala"] = False
            write_products(products)
            print("Wylaczono. Pozycja bedzie znowu wyceniana co tydzien.")
            return
        if raw:
            try:
                save_price(date.today(), pid, parse_money(raw))
            except ValueError:
                print("Nie rozumiem tej kwoty. Anulowano.")
                return
            print("Zapisano nowa cene.")
        return

    raw = input(f"Wlaczam stala cene. Cena (€) [Enter = zostaw ostatnia: {last_txt}]: ").strip()
    if raw:
        try:
            save_price(date.today(), pid, parse_money(raw))
        except ValueError:
            print("Nie rozumiem tej kwoty. Anulowano.")
            return
    elif not last:
        print("Brak jakiejkolwiek ceny - podaj kwote. Anulowano.")
        return
    p["stala"] = True
    write_products(products)
    print("Ustawiono stala cene. Nie bedzie juz pojawiac sie w cotygodniowej rundzie.")


def cmd_history():
    products = read_products()
    if not products:
        print("Brak produktow. Dodaj pierwszy: python tracker.py dodaj")
        return
    default = date.today() - timedelta(days=7)
    raw = input(f"Z jakiej daty sa ceny? RRRR-MM-DD (Enter = {default}): ").strip()
    try:
        day = parse_date(raw) if raw else default
    except ValueError:
        print("Niepoprawna data. Anulowano.")
        return
    if day > date.today():
        print("Data z przyszlosci. Anulowano.")
        return
    prices = read_prices()
    print(f"\nWpisujesz ceny z dnia {day}. Enter = pomin, q = zakoncz.")
    print("Uzywaj tej samej metody co w tygodniowej rundzie (np. srednia z 5 najtanszych ofert EN).\n")
    for i, p in enumerate(products, 1):
        existing = [pr for d, pr in prices.get(p["id"], []) if d == day]
        note = f" (jest juz wpis: {fmt_eur(existing[0])}, zostanie nadpisany)" if existing else ""
        print(f"[{i}/{len(products)}] {p['nazwa']}{note}")
        if ask_and_save_price(p, [], day):
            break
    print()
    build_chart()


def pad_single_date(fig, row, dates):
    """Gdy jest tylko jeden dzien danych, pokazuje os w dniach (a nie w godzinach)."""
    if dates and len(set(dates)) == 1:
        d = dates[0]
        fig.update_xaxes(range=[(d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()],
                         dtick=86400000, tickformat="%Y-%m-%d", row=row, col=1)


# ----------------------------- WYKRES -----------------------------
def build_chart():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import plotly.express as px
    except ImportError:
        print("Brakuje biblioteki plotly. Zainstaluj:  pip install plotly")
        return

    products = read_products()
    prices = read_prices()
    if not products:
        print("Brak produktow - nie ma czego rysowac.")
        return
    sealed = [p for p in products if p["typ"] == SEALED]
    cards = [p for p in products if p["typ"] == CARD]
    colors = px.colors.qualitative.Plotly

    # --- liczenie strefy SEALED ---
    chart_pts = {}
    all_dates = []
    spent_s, value_s, net_s = [], [], []
    sealed_title = ""
    if sealed:
        for p in sealed:
            pts = list(prices.get(p["id"], []))
            if not any(d == p["data_zakupu"] for d, _ in pts):
                pts.append((p["data_zakupu"], p["cena_zakupu"]))  # start = cena zakupu
            chart_pts[p["id"]] = sorted(pts)
        all_dates = sorted({d for pts in chart_pts.values() for d, _ in pts})
        for d in all_dates:
            spent = value = 0.0
            for p in sealed:
                if p["data_zakupu"] > d:
                    continue
                spent += p["ilosc"] * p["cena_zakupu"]
                cur = price_at(chart_pts[p["id"]], d)
                value += p["ilosc"] * (cur if cur is not None else p["cena_zakupu"])
            spent_s.append(spent)
            value_s.append(value)
            net_s.append(value * (1 - FEE_PERCENT / 100))
        t_spent, t_value, t_net = spent_s[-1], value_s[-1], net_s[-1]
        profit = t_net - t_spent
        ppct = profit / t_spent * 100 if t_spent else 0
        label = "po prowizji" if FEE_PERCENT > 0 else ""
        sealed_title = (f"<b>SEALED</b> - wydane {fmt_eur(t_spent)} | wartość {fmt_eur(t_value)} | "
                        f"zwrot {label} {profit:+.2f} € ({ppct:+.1f}%)").replace("  ", " ")

    # --- liczenie strefy KARTY ---
    card_rows = []
    cards_title = ""
    if cards:
        c_start = c_now = 0.0
        for c in cards:
            rec = prices.get(c["id"], [])
            if rec:
                now, ch7, ch30 = changes(rec)
                if c.get("stala"):
                    ch7 = ch30 = None
                start = rec[0][1]
                c_start += start * c["ilosc"]
                c_now += now * c["ilosc"]
            else:
                now = start = ch7 = ch30 = None
            card_rows.append((c, rec, start, now, ch7, ch30))
        gain = c_now - c_start
        cards_title = (f"<b>KARTY</b> - przyrost od startu śledzenia: {gain:+.2f} € "
                       f"({fmt_pct(pct_change(c_now, c_start))}) | wartość teraz {fmt_eur(c_now)}")

    # --- rosnace karty ---
    hot = {}
    for c, rec, start, now, ch7, ch30 in card_rows:
        if c.get("stala") or not rec or now is None or now < HIGHLIGHT_MIN_PRICE:
            continue
        g_start = pct_change(now, start)
        if (g_start is not None and g_start >= HIGHLIGHT_MIN_GAIN) or (ch30 is not None and ch30 >= HIGHLIGHT_MIN_GAIN):
            hot[c["id"]] = g_start

    # --- otwarcia ---
    openings = read_openings()
    if openings:
        o_cost = sum(o["koszt"] for o in openings)
        o_val = sum(o["wartosc_dropu"] for o in openings)
        o_title = (f"<b>OTWARCIA</b> - wydane {fmt_eur(o_cost)} | drop (wartość z dnia otwarcia) {fmt_eur(o_val)} | "
                   f"wynik {o_val - o_cost:+.2f} € ({fmt_pct(pct_change(o_val, o_cost))})")

    # --- uklad wykresu ---
    layout = []  # (typ, tytul, wysokosc w px)
    if sealed:
        layout += [("xy", sealed_title, 420),
                   ("xy", "Sealed: ceny produktów (kropkowana linia = cena zakupu)", 420),
                   ("table", "Sealed: podsumowanie", 70 + 32 * len(sealed))]
    if cards:
        layout += [("xy", cards_title + "<br>Karty: wartość kolekcji w czasie", 420)]
        if hot:
            layout += [("xy", f"▲ Rosnące karty (cena ≥ {HIGHLIGHT_MIN_PRICE:g} €, wzrost ≥ {HIGHLIGHT_MIN_GAIN:g}%): "
                              "zmiana od startu (%)", 380)]
        layout += [("table", "Karty: podsumowanie (rosnące na górze)", 70 + 32 * len(cards))]
    if openings:
        layout += [("xy", o_title + "<br>Otwarcia: koszt vs wartość dropu", 420),
                   ("table", "Otwarcia: lista", 70 + 32 * (len(openings) + 1))]
    total_h = sum(h for _, _, h in layout) + 80 * len(layout)
    fig = make_subplots(
        rows=len(layout), cols=1,
        row_heights=[h / sum(h for _, _, h in layout) for _, _, h in layout],
        vertical_spacing=min(0.07, 0.9 / max(len(layout) - 1, 1)),
        specs=[[{"type": k}] for k, _, _ in layout],
        subplot_titles=[t for _, t, _ in layout],
    )
    row = 0

    # --- SEALED: portfel ---
    if sealed:
        row += 1
        fig.add_trace(go.Scatter(x=all_dates, y=spent_s, name="Wydane", mode="lines+markers",
                                 line=dict(color="#636EFA", width=3, shape="hv"),
                                 hovertemplate="%{x}<br>Wydane: %{y:.2f} €<extra></extra>"), row=row, col=1)
        fig.add_trace(go.Scatter(x=all_dates, y=value_s, name="Wartość rynkowa", mode="lines+markers",
                                 line=dict(color="#00CC96", width=3),
                                 hovertemplate="%{x}<br>Wartość: %{y:.2f} €<extra></extra>"), row=row, col=1)
        if FEE_PERCENT > 0:
            fig.add_trace(go.Scatter(x=all_dates, y=net_s, name=f"Po prowizji {FEE_PERCENT:g}%", mode="lines",
                                     line=dict(color="#EF553B", width=2, dash="dash"),
                                     hovertemplate="%{x}<br>Po prowizji: %{y:.2f} €<extra></extra>"),
                          row=row, col=1)
        fig.update_yaxes(title_text="€", row=row, col=1)
        pad_single_date(fig, row, all_dates)

        # --- SEALED: ceny produktow ---
        row += 1
        d_min, d_max = all_dates[0], all_dates[-1]
        if d_min == d_max:
            d_max = d_min + timedelta(days=1)
        for idx, p in enumerate(sealed):
            color = colors[idx % len(colors)]
            pts = chart_pts[p["id"]]
            fig.add_trace(go.Scatter(x=[d for d, _ in pts], y=[v for _, v in pts], name=p["nazwa"],
                                     legendgroup="s" + p["nazwa"], mode="lines+markers", line=dict(color=color),
                                     hovertemplate=p["nazwa"] + "<br>%{x}: %{y:.2f} €<extra></extra>"),
                          row=row, col=1)
            fig.add_trace(go.Scatter(x=[d_min, d_max], y=[p["cena_zakupu"]] * 2, legendgroup="s" + p["nazwa"],
                                     showlegend=False, mode="lines", line=dict(color=color, dash="dot", width=1),
                                     hovertemplate=p["nazwa"] + "<br>cena zakupu: %{y:.2f} €<extra></extra>"),
                          row=row, col=1)
        fig.update_yaxes(title_text="€ za sztukę", row=row, col=1)

        # --- SEALED: tabela ---
        row += 1
        cols = {k: [] for k in ["Produkt", "Szt.", "Kupno", "Teraz", "Wartość", "Zwrot €", "Zwrot %",
                                "7 dni", "30 dni", "Trend"]}
        for p in sealed:
            rec = prices.get(p["id"], [])
            if rec:
                now, ch7, ch30 = changes(rec)
            else:
                now, ch7, ch30 = p["cena_zakupu"], None, None
            val = now * p["ilosc"]
            cost = p["cena_zakupu"] * p["ilosc"]
            net = val * (1 - FEE_PERCENT / 100)
            cols["Produkt"].append(p["nazwa"])
            cols["Szt."].append(p["ilosc"])
            cols["Kupno"].append(fmt_eur(p["cena_zakupu"]))
            cols["Teraz"].append(fmt_eur(now) + ("" if rec else " (brak wpisów)"))
            cols["Wartość"].append(fmt_eur(val))
            cols["Zwrot €"].append(f"{net - cost:+.2f} €")
            cols["Zwrot %"].append(fmt_pct(pct_change(net, cost)))
            cols["7 dni"].append(fmt_pct(ch7))
            cols["30 dni"].append(fmt_pct(ch30))
            cols["Trend"].append(trend_label(ch7, ch30))
        fig.add_trace(go.Table(header=dict(values=list(cols.keys()), fill_color="#2a3f5f",
                                           font=dict(color="white"), align="left"),
                               cells=dict(values=list(cols.values()), align="left", height=28)),
                      row=row, col=1)

    # --- KARTY: wartosc kolekcji w czasie ---
    if cards:
        row += 1
        card_dates = sorted({d for _, rec, *_ in card_rows for d, _ in rec})
        if card_dates:
            total_s, base_s = [], []
            for d in card_dates:
                tot = base = 0.0
                for c, rec, start, now, ch7, ch30 in card_rows:
                    if not rec or rec[0][0] > d:
                        continue  # karta jeszcze nie sledzona w tym dniu
                    tot += c["ilosc"] * price_at(rec, d)
                    base += c["ilosc"] * start
                total_s.append(tot)
                base_s.append(base)
            fig.add_trace(go.Scatter(
                x=card_dates, y=total_s, name="Wartość kart", mode="lines+markers",
                line=dict(color="#00CC96", width=4), marker=dict(size=10),
                hovertemplate="%{x}<br>Wartość kart: %{y:.2f} €<extra></extra>"), row=row, col=1)
            fig.add_trace(go.Scatter(
                x=card_dates, y=base_s, name="Wartość wg cen startowych (bez przyrostu)", mode="lines",
                line=dict(color="gray", width=2, dash="dash", shape="hv"),
                hovertemplate="%{x}<br>Wg cen startowych: %{y:.2f} €<extra></extra>"), row=row, col=1)
            # pojedyncze karty - domyslnie ukryte, wlaczasz klikajac nazwe w legendzie
            for idx, (c, rec, start, now, ch7, ch30) in enumerate(card_rows):
                if not rec:
                    continue
                fig.add_trace(go.Scatter(
                    x=[d for d, _ in rec], y=[c["ilosc"] * v for _, v in rec], name=c["nazwa"],
                    visible="legendonly", mode="lines+markers", line=dict(width=1.5),
                    hovertemplate=c["nazwa"] + "<br>%{x}: %{y:.2f} €<extra></extra>"), row=row, col=1)
            pad_single_date(fig, row, card_dates)
        fig.update_yaxes(title_text="€ (cała kolekcja kart)", row=row, col=1)

        # --- KARTY: rosnace ---
        if hot:
            row += 1
            for c, rec, start, now, ch7, ch30 in card_rows:
                if c["id"] not in hot:
                    continue
                fig.add_trace(go.Scatter(
                    x=[d for d, _ in rec], y=[(v - start) / start * 100 for _, v in rec],
                    name="▲ " + c["nazwa"], mode="lines+markers", line=dict(width=3), marker=dict(size=9),
                    customdata=[v for _, v in rec],
                    hovertemplate=c["nazwa"] + "<br>%{x}: %{customdata:.2f} € (%{y:+.1f}% od startu)<extra></extra>"),
                    row=row, col=1)
            hot_dates = sorted({d for c, rec, *_ in card_rows if c["id"] in hot for d, _ in rec})
            pad_single_date(fig, row, hot_dates)
            fig.update_yaxes(title_text="% od startu", row=row, col=1)

        # --- KARTY: tabela (rosnace na gorze, podswietlone) ---
        row += 1
        cols = {k: [] for k in ["Karta", "Szt.", "Start", "Teraz", "Przyrost €", "Przyrost %",
                                "7 dni", "30 dni", "Trend"]}
        fills = []
        for c, rec, start, now, ch7, ch30 in sorted(card_rows, key=lambda r: r[0]["id"] not in hot):
            is_hot = c["id"] in hot
            fills.append("#d9f7e4" if is_hot else "white")
            cols["Karta"].append(("▲ " if is_hot else "") + c["nazwa"])
            cols["Szt."].append(c["ilosc"])
            if rec:
                cols["Start"].append(f"{fmt_eur(start)} ({rec[0][0]})")
                cols["Teraz"].append(fmt_eur(now))
                cols["Przyrost €"].append(f"{(now - start) * c['ilosc']:+.2f} €")
                cols["Przyrost %"].append(fmt_pct(pct_change(now, start)))
                cols["7 dni"].append(fmt_pct(ch7))
                cols["30 dni"].append(fmt_pct(ch30))
                if is_hot:
                    cols["Trend"].append("▲ ROŚNIE")
                else:
                    cols["Trend"].append("stała cena" if c.get("stala") else trend_label(ch7, ch30))
            else:
                for k in ["Start", "Teraz", "Przyrost €", "Przyrost %", "7 dni", "30 dni"]:
                    cols[k].append("–")
                cols["Trend"].append("brak wpisów")
        fig.add_trace(go.Table(header=dict(values=list(cols.keys()), fill_color="#2a3f5f",
                                           font=dict(color="white"), align="left"),
                               cells=dict(values=list(cols.values()), fill_color=[fills] * len(cols),
                                          align="left", height=28)),
                      row=row, col=1)

    # --- OTWARCIA ---
    if openings:
        row += 1
        labels = [f"#{o['id']} {o['nazwa']}<br>{o['data']}" for o in openings] + ["RAZEM"]
        costs = [o["koszt"] for o in openings] + [o_cost]
        vals = [o["wartosc_dropu"] for o in openings] + [o_val]
        fig.add_trace(go.Bar(x=labels, y=costs, name="Koszt otwarcia", marker_color="#EF553B",
                             hovertemplate="%{x}<br>Koszt: %{y:.2f} €<extra></extra>"), row=row, col=1)
        fig.add_trace(go.Bar(x=labels, y=vals, name="Wartość dropu", marker_color="#00CC96",
                             hovertemplate="%{x}<br>Drop: %{y:.2f} €<extra></extra>"), row=row, col=1)
        fig.update_yaxes(title_text="€", row=row, col=1)
        fig.update_layout(barmode="group")

        row += 1
        ocols = {k: [] for k in ["Otwarcie", "Data", "Koszt", "Drop (wartość z dnia)", "Wynik €", "Wynik %"]}
        ofills = []
        for o in openings + [{"nazwa": "RAZEM", "data": "", "koszt": o_cost, "wartosc_dropu": o_val}]:
            res = o["wartosc_dropu"] - o["koszt"]
            ofills.append("#d9f7e4" if res >= 0 else "#fde2e2")
            ocols["Otwarcie"].append(o["nazwa"])
            ocols["Data"].append(str(o["data"]))
            ocols["Koszt"].append(fmt_eur(o["koszt"]))
            ocols["Drop (wartość z dnia)"].append(fmt_eur(o["wartosc_dropu"]))
            ocols["Wynik €"].append(f"{res:+.2f} €")
            ocols["Wynik %"].append(fmt_pct(pct_change(o["wartosc_dropu"], o["koszt"])))
        fig.add_trace(go.Table(header=dict(values=list(ocols.keys()), fill_color="#2a3f5f",
                                           font=dict(color="white"), align="left"),
                               cells=dict(values=list(ocols.values()), fill_color=[ofills] * len(ocols),
                                          align="left", height=28)),
                      row=row, col=1)

    fig.update_layout(height=total_h, hovermode="closest", legend=dict(orientation="h", y=-0.02),
                      title=dict(text="Pokemon TCG - tracker cen", x=0.5))
    fig.write_html(CHART_FILE, include_plotlyjs=True)
    print(f"Wykres zapisany: {CHART_FILE}")
    if sealed:
        print(f"SEALED: wydane {fmt_eur(t_spent)} | wartość {fmt_eur(t_value)} | zwrot {profit:+.2f} € ({ppct:+.1f}%)")
    if cards:
        print(f"KARTY: przyrost od startu {gain:+.2f} € | wartość teraz {fmt_eur(c_now)}"
              + (f" | rosnące: {len(hot)}" if hot else ""))
    if openings:
        print(f"OTWARCIA: wydane {fmt_eur(o_cost)} | drop {fmt_eur(o_val)} | wynik {o_val - o_cost:+.2f} €")
    webbrowser.open(CHART_FILE.as_uri())


# ----------------------------- START -----------------------------
def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        if cmd in ("dodaj", "add"):
            cmd_add()
        elif cmd in ("stala", "stała", "fixed"):
            cmd_fixed()
        elif cmd in ("otworz", "otwórz", "open"):
            cmd_open()
        elif cmd in ("usun", "usuń", "delete"):
            cmd_delete()
        elif cmd in ("historia", "history"):
            cmd_history()
        elif cmd in ("wykres", "chart"):
            build_chart()
        elif cmd in ("lista", "list"):
            cmd_list()
        elif cmd == "sealed":
            cmd_weekly(SEALED)
        elif cmd in ("karty", "karta", "cards"):
            cmd_weekly(CARD)
        elif cmd == "":
            cmd_weekly()
        else:
            print(__doc__)
    except (KeyboardInterrupt, EOFError):
        print("\nPrzerwano.")


if __name__ == "__main__":
    main()