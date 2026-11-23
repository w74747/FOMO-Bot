"""Arabic labels for the scanner's rejection reasons (shown in Telegram)."""

AR = [
    ("history too short", "تاريخ تداول قصير"),
    ("only", "موجات مكتملة قليلة"),
    ("waves too small", "موجات صغيرة لا تغطي التكاليف"),
    ("waves too violent", "موجات عنيفة جداً"),
    ("waves too slow", "موجات بطيئة جداً (الدورة أطول من 3 أيام)"),
    ("downtrend", "اتجاه هابط"),
    ("lower lows", "قيعان متناقصة (العملة تنزف)"),
    ("reward/risk too low", "الربح المتوقع لا يغطي المخاطرة بعد التكاليف"),
    ("not enough backtest evidence", "دليل تاريخي غير كافٍ"),
    ("backtest negative", "الاختبار التاريخي خاسر"),
    ("backtest median trade not positive", "الصفقة المعتادة في الاختبار غير رابحة (الوسيط ≤ 0)"),
    ("score", "التقييم أقل من الحد"),
    ("top holders own", "تركيز ملاك عالٍ"),
    ("top-10 holders own", "تركيز ملاك عالٍ"),
    ("mint authority active", "صلاحية السك مفعّلة (يمكن طباعة المزيد)"),
    ("freeze authority active", "صلاحية التجميد مفعّلة"),
    ("transfer fee", "رسوم تحويل على العملة"),
    ("Token-2022 extension", "امتداد Token-2022 خطر"),
    ("too young", "عمرها أقل من 7 أيام"),
    ("low liquidity", "سيولة منخفضة"),
    ("low volume", "حجم تداول منخفض"),
    ("fdv too high", "قيمة سوقية مرتفعة جداً"),
    ("few holders", "عدد ملاك قليل"),
    ("low organic score", "تداول غير عضوي (وهمي محتمل)"),
    ("concentrated holders", "تركيز ملاك عالٍ"),
    ("flagged suspicious", "مصنفة مشبوهة"),
    ("major/stable", "عملة رئيسية أو مستقرة"),
    ("unsupported quote", "زوج تداول غير مدعوم"),
    ("no supported pool", "لا يوجد مجمع سيولة مدعوم"),
    ("not enough candles", "بيانات غير كافية"),
    ("no candles", "تعذّر جلب البيانات"),
]


def ar_reason(reason: str) -> str:
    """Translate one reason, keeping any numbers in parentheses."""
    for key, label in AR:
        if reason.startswith(key):
            detail = reason[len(key):].strip()
            if "(" in reason:
                detail = reason[reason.index("("):]
            elif key == "only":
                detail = f"({detail.split()[0]})" if detail else ""
            elif key in ("downtrend", "lower lows", "score", "top holders own", "top-10 holders own",
                         "transfer fee"):
                detail = f"({detail})" if detail else ""
            else:
                detail = ""
            return f"{label} {detail}".strip()
    return reason
