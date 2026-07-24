/* JarLang native runtime: printing, strings, lists, math, and errors for the compiled code */
/* Memory is never freed, since the programs are short-lived */
#if defined(__MINGW32__) || defined(__MINGW64__)
#define __USE_MINGW_ANSI_STDIO 1
#endif
#include <ctype.h>
#include <errno.h>
#include <float.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include <io.h>
#include <windows.h>
#define JL_ISATTY(fd) _isatty(fd)
#define JL_FILENO(f) _fileno(f)
#else
#include <unistd.h>
#define JL_ISATTY(fd) isatty(fd)
#define JL_FILENO(f) fileno(f)
#endif

typedef int64_t i64;

#if defined(__GNUC__) || defined(__clang__)
#define NORETURN __attribute__((noreturn))
#else
#define NORETURN
#endif

extern const char jl_source_name[];
extern i64 jl_main(void);

/* Memory */

static void *jl_alloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (!p) {
        fflush(stdout);
        fputs("error: out of memory\n", stderr);
        exit(1);
    }
    return p;
}

static void *jl_realloc(void *old, size_t n) {
    void *p = realloc(old, n ? n : 1);
    if (!p) {
        fflush(stdout);
        fputs("error: out of memory\n", stderr);
        exit(1);
    }
    return p;
}

static char *jl_strdup_len(const char *s, size_t n) {
    char *out = jl_alloc(n + 1);
    memcpy(out, s, n);
    out[n] = 0;
    return out;
}

/* Errors */

NORETURN void jl_panic(const char *msg, i64 line) {
    fflush(stdout);
    fprintf(stderr, "error: %s\n --> %s:%" PRId64 "\n", msg, jl_source_name, line);
    exit(1);
}

NORETURN void jl_panic_overflow(i64 line) {
    jl_panic("integer overflow (native integers are 64-bit; the interpreter supports big integers)", line);
}

NORETURN void jl_panic_divzero(i64 line) { jl_panic("division by zero", line); }

NORETURN void jl_panic_modzero(i64 line) { jl_panic("modulo by zero", line); }

NORETURN void jl_panic_assert(const char *msg, i64 line) {
    jl_panic(msg ? msg : "assertion failed", line);
}

/* Float formatting (matches Python's repr) */

/* Shortest digits that round-trip (x = 0.DIGITS * 10^decpt), returns the digit count */
static int jl_shortest_digits(double x, char *digits, int *decpt) {
    char buf[64];
    int prec;
    for (prec = 1; prec <= 17; prec++) {
        snprintf(buf, sizeof buf, "%.*e", prec - 1, x);
        if (strtod(buf, NULL) == x) break;
    }
    /* buf looks like "1.2345e+05" (x is positive here) */
    int n = 0;
    const char *p = buf;
    while (*p && *p != 'e') {
        if (*p >= '0' && *p <= '9') digits[n++] = *p;
        p++;
    }
    int exp10 = (*p == 'e') ? atoi(p + 1) : 0;
    while (n > 1 && digits[n - 1] == '0') n--;
    digits[n] = 0;
    *decpt = exp10 + 1;
    return n;
}

/* Write repr(x) into out (at least 40 bytes) */
static int jl_repr_double(double x, char *out) {
    if (isnan(x)) return sprintf(out, "nan");
    if (isinf(x)) return sprintf(out, x > 0 ? "inf" : "-inf");
    if (x == 0) return sprintf(out, signbit(x) ? "-0.0" : "0.0");
    char digits[32];
    int decpt;
    char *o = out;
    if (x < 0) {
        *o++ = '-';
        x = -x;
    }
    int n = jl_shortest_digits(x, digits, &decpt);
    if (decpt > -4 && decpt <= 16) {
        if (decpt <= 0) {
            *o++ = '0';
            *o++ = '.';
            for (int i = 0; i < -decpt; i++) *o++ = '0';
            memcpy(o, digits, n);
            o += n;
        } else if (decpt >= n) {
            memcpy(o, digits, n);
            o += n;
            for (int i = 0; i < decpt - n; i++) *o++ = '0';
            *o++ = '.';
            *o++ = '0';
        } else {
            memcpy(o, digits, decpt);
            o += decpt;
            *o++ = '.';
            memcpy(o, digits + decpt, n - decpt);
            o += n - decpt;
        }
    } else {
        *o++ = digits[0];
        if (n > 1) {
            *o++ = '.';
            memcpy(o, digits + 1, n - 1);
            o += n - 1;
        }
        int e = decpt - 1;
        o += sprintf(o, "e%c%02d", e < 0 ? '-' : '+', e < 0 ? -e : e);
    }
    *o = 0;
    return (int)(o - out);
}

/* String builder */

typedef struct {
    char *buf;
    size_t len, cap;
} JlSb;

typedef struct {
    i64 len;
    i64 cap;
    i64 *data;
} JlList;

JlList *jl_list_new(i64 cap);
JlList *jl_list_push(JlList *l, i64 bits);
JlList *jl_list_concat(JlList *a, JlList *b);

JlSb *jl_sb_new(void) {
    JlSb *sb = jl_alloc(sizeof *sb);
    sb->cap = 32;
    sb->len = 0;
    sb->buf = jl_alloc(sb->cap);
    sb->buf[0] = 0;
    return sb;
}

static void sb_append_n(JlSb *sb, const char *s, size_t n) {
    if (sb->len + n + 1 > sb->cap) {
        while (sb->len + n + 1 > sb->cap) sb->cap *= 2;
        sb->buf = jl_realloc(sb->buf, sb->cap);
    }
    memcpy(sb->buf + sb->len, s, n);
    sb->len += n;
    sb->buf[sb->len] = 0;
}

static void sb_append(JlSb *sb, const char *s) { sb_append_n(sb, s, strlen(s)); }

static void sb_putc(JlSb *sb, char c) { sb_append_n(sb, &c, 1); }

void jl_sb_str(JlSb *sb, const char *s) { sb_append(sb, s); }

void jl_sb_int(JlSb *sb, i64 v) {
    char buf[32];
    snprintf(buf, sizeof buf, "%" PRId64, v);
    sb_append(sb, buf);
}

void jl_sb_float(JlSb *sb, double x) {
    char buf[48];
    jl_repr_double(x, buf);
    sb_append(sb, buf);
}

void jl_sb_bool(JlSb *sb, i64 b) { sb_append(sb, b ? "true" : "false"); }

void jl_sb_nil(JlSb *sb) { sb_append(sb, "nil"); }

static void sb_quoted(JlSb *sb, const char *s) {
    sb_putc(sb, '"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        char esc[16];
        switch (*p) {
            case '"': sb_append(sb, "\\\""); break;
            case '\\': sb_append(sb, "\\\\"); break;
            case '\n': sb_append(sb, "\\n"); break;
            case '\t': sb_append(sb, "\\t"); break;
            case '{': sb_append(sb, "\\{"); break;
            case '}': sb_append(sb, "\\}"); break;
            default:
                if (*p < 32) {
                    snprintf(esc, sizeof esc, "\\u{%x}", *p);
                    sb_append(sb, esc);
                } else {
                    sb_putc(sb, (char)*p);
                }
        }
    }
    sb_putc(sb, '"');
}

static const char *sb_value(JlSb *sb, i64 bits, const char *desc);

void jl_sb_list(JlSb *sb, JlList *l, const char *desc) {
    sb_putc(sb, '[');
    for (i64 i = 0; i < l->len; i++) {
        if (i) sb_append(sb, ", ");
        sb_value(sb, l->data[i], desc);
    }
    sb_putc(sb, ']');
}

/* Append one value described by desc and return the rest of desc */
static const char *sb_value(JlSb *sb, i64 bits, const char *desc) {
    double d;
    switch (desc[0]) {
        case 'i': jl_sb_int(sb, bits); return desc + 1;
        case 'f': memcpy(&d, &bits, 8); jl_sb_float(sb, d); return desc + 1;
        case 'b': jl_sb_bool(sb, bits); return desc + 1;
        case 's': sb_quoted(sb, (const char *)(intptr_t)bits); return desc + 1;
        case 'n': jl_sb_nil(sb); return desc + 1;
        case 'L': jl_sb_list(sb, (JlList *)(intptr_t)bits, desc + 1); return desc + 1;
        default: jl_sb_int(sb, bits); return desc + 1;
    }
}

const char *jl_sb_finish(JlSb *sb) { return sb->buf; }

/* Format specs (a subset of Python's format mini-language) */

typedef struct {
    char fill[5];
    char align;     /* '<' '>' '^' '=' */
    char sign;      /* '+' '-' ' ' or 0 */
    int width;
    char grouping;  /* ',' '_' or 0 */
    int precision;  /* -1 when absent */
    char type;      /* 0 when absent */
} JlSpec;

/* default_align is '>' for numbers and '<' for strings, like Python */
static void parse_spec(const char *s, JlSpec *sp, char default_align) {
    memset(sp, 0, sizeof *sp);
    sp->precision = -1;
    strcpy(sp->fill, " ");
    int fill_given = 0;
    /* Fill character (can be UTF-8) followed by align */
    int clen = 1;
    unsigned char c0 = (unsigned char)s[0];
    if (c0 >= 0xF0) clen = 4; else if (c0 >= 0xE0) clen = 3; else if (c0 >= 0xC0) clen = 2;
    if (s[0] && strchr("<>^=", s[clen]) && s[clen]) {
        memcpy(sp->fill, s, clen);
        sp->fill[clen] = 0;
        sp->align = s[clen];
        fill_given = 1;
        s += clen + 1;
    } else if (s[0] && strchr("<>^=", s[0])) {
        sp->align = s[0];
        s++;
    }
    if (*s && strchr("+- ", *s)) sp->sign = *s++;
    /* A 0 before the width fills with zeros. Numbers without an align also pad after the sign */
    if (*s == '0' && !fill_given) {
        strcpy(sp->fill, "0");
        if (!sp->align && default_align == '>') sp->align = '=';
        s++;
    }
    if (!sp->align) sp->align = default_align;
    while (isdigit((unsigned char)*s)) sp->width = sp->width * 10 + (*s++ - '0');
    if (*s == ',' || *s == '_') sp->grouping = *s++;
    if (*s == '.') {
        s++;
        sp->precision = 0;
        while (isdigit((unsigned char)*s)) sp->precision = sp->precision * 10 + (*s++ - '0');
    }
    if (*s) sp->type = *s;
}

static size_t utf8_len(const char *s) {
    size_t n = 0;
    for (; *s; s++)
        if (((unsigned char)*s & 0xC0) != 0x80) n++;
    return n;
}

/* Pad body (with an optional sign or prefix) to the spec's width */
static void sb_padded(JlSb *sb, const char *prefix, const char *body, const JlSpec *sp) {
    size_t len = utf8_len(prefix) + utf8_len(body);
    size_t pad = (sp->width > 0 && (size_t)sp->width > len) ? (size_t)sp->width - len : 0;
    size_t left = 0, right = 0;
    switch (sp->align) {
        case '<': right = pad; break;
        case '^': left = pad / 2; right = pad - left; break;
        default: left = pad; break;
    }
    if (sp->align == '=') {
        sb_append(sb, prefix);
        for (size_t i = 0; i < left; i++) sb_append(sb, sp->fill);
        sb_append(sb, body);
        return;
    }
    for (size_t i = 0; i < left; i++) sb_append(sb, sp->fill);
    sb_append(sb, prefix);
    sb_append(sb, body);
    for (size_t i = 0; i < right; i++) sb_append(sb, sp->fill);
}

/* Group the digits at the start of body and return a new string. With a 0 fill and '=' align,
   Python pads the digits themselves, so "{1.5:08,.1f}" is 00,001.5 */
static char *group_body(const char *body, const JlSpec *sp, int every, size_t prefix_len) {
    const char *end = body;
    while (*end && (every == 3 ? isdigit((unsigned char)*end) : isxdigit((unsigned char)*end))) end++;
    size_t ndig = (size_t)(end - body), want = ndig, rest = strlen(end);
    if (sp->align == '=' && strcmp(sp->fill, "0") == 0 && (size_t)sp->width > prefix_len + rest) {
        size_t target = (size_t)sp->width - prefix_len - rest;
        if (want == 0) want = 1;
        while (want + (want - 1) / (size_t)every < target) want++;
    }
    char *out = jl_alloc(want + want / (size_t)every + rest + 1);
    char *o = out;
    size_t first = want % (size_t)every ? want % (size_t)every : (size_t)every;
    for (size_t i = 0; i < want; i++) {
        if (i >= first && (i - first) % (size_t)every == 0) *o++ = sp->grouping;
        *o++ = i < want - ndig ? '0' : body[i - (want - ndig)];
    }
    strcpy(o, end);
    return out;
}

static void sb_float_spec(JlSb *sb, double x, const JlSpec *sp);

void jl_sb_int_fmt(JlSb *sb, i64 v, const char *spec, i64 line) {
    JlSpec sp;
    parse_spec(spec, &sp, '>');
    if (sp.type && strchr("eEfFgG%", sp.type)) {
        sb_float_spec(sb, (double)v, &sp);
        return;
    }
    char digits[96];
    uint64_t mag = v < 0 ? (uint64_t)0 - (uint64_t)v : (uint64_t)v;
    int base = 10;
    int every = 3;
    switch (sp.type) {
        case 'x': case 'X': base = 16; every = 4; break;
        case 'o': base = 8; every = 4; break;
        case 'b': base = 2; every = 4; break;
        case 'c': {
            if (v < 0 || v > 0x10FFFF) jl_panic("invalid format spec 'c' for int", line);
            char ch[8] = {0};
            uint64_t cp = mag;
            if (cp < 0x80) { ch[0] = (char)cp; }
            else if (cp < 0x800) { ch[0] = (char)(0xC0 | (cp >> 6)); ch[1] = (char)(0x80 | (cp & 0x3F)); }
            else if (cp < 0x10000) { ch[0] = (char)(0xE0 | (cp >> 12)); ch[1] = (char)(0x80 | ((cp >> 6) & 0x3F)); ch[2] = (char)(0x80 | (cp & 0x3F)); }
            else { ch[0] = (char)(0xF0 | (cp >> 18)); ch[1] = (char)(0x80 | ((cp >> 12) & 0x3F)); ch[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); ch[3] = (char)(0x80 | (cp & 0x3F)); }
            sb_padded(sb, "", ch, &sp);
            return;
        }
        default: break;
    }
    const char *alphabet = sp.type == 'X' ? "0123456789ABCDEF" : "0123456789abcdef";
    char tmp[96];
    int n = 0;
    do {
        tmp[n++] = alphabet[mag % (uint64_t)base];
        mag /= (uint64_t)base;
    } while (mag);
    for (int i = 0; i < n; i++) digits[i] = tmp[n - 1 - i];
    digits[n] = 0;
    const char *prefix = "";
    if (v < 0) prefix = "-";
    else if (sp.sign == '+') prefix = "+";
    else if (sp.sign == ' ') prefix = " ";
    if (sp.grouping) {
        char *grouped = group_body(digits, &sp, every, strlen(prefix));
        sb_padded(sb, prefix, grouped, &sp);
        free(grouped);
        return;
    }
    sb_padded(sb, prefix, digits, &sp);
}

/* snprintf with a precision and a double, into a buffer as big as the result (like {1e300:.300f}) */
static char *fmt_alloc(const char *fmt, int prec, double x) {
    int n = snprintf(NULL, 0, fmt, prec, x);
    char *out = jl_alloc((size_t)n + 1);
    snprintf(out, (size_t)n + 1, fmt, prec, x);
    return out;
}

/* Python's format(x, ".3") with no type: like 'g', but scientific from exponent p - 1 on,
   and fixed notation always keeps a decimal point */
static char *general_no_type(double ax, int p) {
    if (p == 0) p = 1;
    char *s = fmt_alloc("%.*e", p - 1, ax);
    char *e = strchr(s, 'e');
    int exp = atoi(e + 1);
    if (exp < -4 || exp >= p - 1) {
        char *m = e;
        if (strchr(s, '.')) {
            while (m[-1] == '0') m--;
            if (m[-1] == '.') m--;
        }
        memmove(m, e, strlen(e) + 1);
        return s;
    }
    free(s);
    s = fmt_alloc("%.*g", p, ax);
    if (!strchr(s, '.')) {
        size_t n = strlen(s);
        s = jl_realloc(s, n + 3);
        memcpy(s + n, ".0", 3);
    }
    return s;
}

static void sb_float_spec(JlSb *sb, double x, const JlSpec *sp) {
    int neg = signbit(x) && !isnan(x);
    int finite = !isnan(x) && !isinf(x);
    double ax = fabs(x);
    char type = sp->type;
    int prec = sp->precision;
    char *body;
    if (!finite) {
        body = jl_alloc(8);
        strcpy(body, isnan(x) ? "nan" : "inf");
        if (type == 'F' || type == 'E' || type == 'G') {
            for (char *p = body; *p; p++) *p = (char)toupper((unsigned char)*p);
        }
        if (type == '%') strcat(body, "%");
    } else if (type == '%') {
        body = fmt_alloc("%.*f%%", prec < 0 ? 6 : prec, ax * 100.0);
    } else if (type == 'f' || type == 'F' || type == 'e' || type == 'E') {
        char fmt[8] = {'%', '.', '*', type, 0};
        body = fmt_alloc(fmt, prec < 0 ? 6 : prec, ax);
    } else if (type == 'g' || type == 'G') {
        char fmt[8] = {'%', '.', '*', type, 0};
        body = fmt_alloc(fmt, prec < 0 ? 6 : (prec == 0 ? 1 : prec), ax);
    } else if (prec >= 0) {
        body = general_no_type(ax, prec);
    } else {
        body = jl_alloc(48);
        jl_repr_double(ax, body);
    }
    const char *prefix = "";
    if (neg) prefix = "-";
    else if (sp->sign == '+') prefix = "+";
    else if (sp->sign == ' ') prefix = " ";
    if (sp->grouping && finite) {
        char *grouped = group_body(body, sp, 3, strlen(prefix));
        free(body);
        body = grouped;
    }
    sb_padded(sb, prefix, body, sp);
    free(body);
}

void jl_sb_float_fmt(JlSb *sb, double x, const char *spec) {
    JlSpec sp;
    parse_spec(spec, &sp, '>');
    sb_float_spec(sb, x, &sp);
}

void jl_sb_str_fmt(JlSb *sb, const char *s, const char *spec) {
    JlSpec sp;
    parse_spec(spec, &sp, '<');
    if (sp.precision >= 0) {
        /* Truncate to precision code points */
        const char *p = s;
        int count = 0;
        while (*p && count < sp.precision) {
            p++;
            while (*p && ((unsigned char)*p & 0xC0) == 0x80) p++;
            count++;
        }
        char *cut = jl_strdup_len(s, (size_t)(p - s));
        sb_padded(sb, "", cut, &sp);
        free(cut);
        return;
    }
    sb_padded(sb, "", s, &sp);
}

void jl_sb_bool_fmt(JlSb *sb, i64 b, const char *spec) { jl_sb_str_fmt(sb, b ? "true" : "false", spec); }

/* Printing */

void jl_print_int(i64 v) { printf("%" PRId64, v); }

void jl_print_float(double x) {
    char buf[48];
    jl_repr_double(x, buf);
    fputs(buf, stdout);
}

void jl_print_bool(i64 b) { fputs(b ? "true" : "false", stdout); }

void jl_print_str(const char *s) { fputs(s, stdout); }

void jl_print_nil(void) { fputs("nil", stdout); }

void jl_print_space(void) { putchar(' '); }

void jl_print_newline(void) { putchar('\n'); }

void jl_print_list(JlList *l, const char *desc) {
    JlSb *sb = jl_sb_new();
    jl_sb_list(sb, l, desc);
    fputs(sb->buf, stdout);
    free(sb->buf);
    free(sb);
}

/* Strings */

const char *jl_str_concat(const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    char *out = jl_alloc(la + lb + 1);
    memcpy(out, a, la);
    memcpy(out + la, b, lb + 1);
    return out;
}

/* Same limit and message as the interpreter, which also keeps len * n from overflowing */
static void check_repeat(i64 len, i64 n, i64 line) {
    if (n <= 0) return;
    unsigned __int128 total = (unsigned __int128)(uint64_t)len * (uint64_t)n;
    if (total <= 100000000) return;
    char rev[64], num[64], msg[128];
    int k = 0, o = 0;
    do {
        rev[k++] = (char)('0' + (int)(total % 10));
        total /= 10;
    } while (total);
    while (k--) {
        num[o++] = rev[k];
        if (k && k % 3 == 0) num[o++] = ',';
    }
    num[o] = 0;
    snprintf(msg, sizeof msg, "result of `*` is too large (%s items)", num);
    jl_panic(msg, line);
}

const char *jl_str_repeat(const char *s, i64 n, i64 line) {
    check_repeat((i64)utf8_len(s), n, line);
    if (n <= 0) return "";
    size_t len = strlen(s);
    char *out = jl_alloc(len * (size_t)n + 1);
    for (i64 i = 0; i < n; i++) memcpy(out + (size_t)i * len, s, len);
    out[len * (size_t)n] = 0;
    return out;
}

i64 jl_str_eq(const char *a, const char *b) { return strcmp(a, b) == 0; }

i64 jl_str_cmp(const char *a, const char *b) {
    int c = strcmp(a, b);
    return (c > 0) - (c < 0);
}

i64 jl_str_len(const char *s) { return (i64)utf8_len(s); }

i64 jl_str_truthy(const char *s) { return s[0] != 0; }

i64 jl_str_contains(const char *hay, const char *needle) { return strstr(hay, needle) != NULL; }

const char *jl_str_index(const char *s, i64 i, i64 line) {
    i64 n = (i64)utf8_len(s);
    i64 orig = i;
    if (i < 0) i += n;
    if (i < 0 || i >= n) {
        char msg[128];
        snprintf(msg, sizeof msg, "index %" PRId64 " is out of range for str of length %" PRId64, orig, n);
        jl_panic(msg, line);
    }
    const char *p = s;
    for (i64 k = 0; k < i; k++) {
        p++;
        while (((unsigned char)*p & 0xC0) == 0x80) p++;
    }
    const char *q = p + 1;
    while (*q && ((unsigned char)*q & 0xC0) == 0x80) q++;
    return jl_strdup_len(p, (size_t)(q - p));
}

const char *jl_str_upper(const char *s) {
    size_t n = strlen(s);
    char *out = jl_strdup_len(s, n);
    for (size_t i = 0; i < n; i++)
        if (out[i] >= 'a' && out[i] <= 'z') out[i] = (char)(out[i] - 32);
    return out;
}

const char *jl_str_lower(const char *s) {
    size_t n = strlen(s);
    char *out = jl_strdup_len(s, n);
    for (size_t i = 0; i < n; i++)
        if (out[i] >= 'A' && out[i] <= 'Z') out[i] = (char)(out[i] + 32);
    return out;
}

const char *jl_str_trim(const char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) n--;
    return jl_strdup_len(s, n);
}

/* Split like Python's str.split(), on whitespace when sep is NULL */
JlList *jl_str_split(const char *s, const char *sep) {
    JlList *out = jl_list_new(4);
    if (sep == NULL) {
        const char *p = s;
        for (;;) {
            while (*p && isspace((unsigned char)*p)) p++;
            if (!*p) break;
            const char *start = p;
            while (*p && !isspace((unsigned char)*p)) p++;
            jl_list_push(out, (i64)(intptr_t)jl_strdup_len(start, (size_t)(p - start)));
        }
        return out;
    }
    size_t seplen = strlen(sep);
    if (seplen == 0) {  /* Split into characters like the interpreter */
        const char *p = s;
        while (*p) {
            const char *q = p + 1;
            while (*q && ((unsigned char)*q & 0xC0) == 0x80) q++;
            jl_list_push(out, (i64)(intptr_t)jl_strdup_len(p, (size_t)(q - p)));
            p = q;
        }
        return out;
    }
    const char *p = s;
    for (;;) {
        const char *hit = strstr(p, sep);
        if (!hit) {
            jl_list_push(out, (i64)(intptr_t)jl_strdup_len(p, strlen(p)));
            return out;
        }
        jl_list_push(out, (i64)(intptr_t)jl_strdup_len(p, (size_t)(hit - p)));
        p = hit + seplen;
    }
}

JlList *jl_str_chars(const char *s) { return jl_str_split(s, ""); }

/* Strings are joined as they are, other values as print() shows them */
const char *jl_list_join(JlList *l, const char *desc, const char *sep) {
    JlSb *sb = jl_sb_new();
    for (i64 i = 0; i < l->len; i++) {
        if (i) sb_append(sb, sep);
        if (desc[0] == 's') sb_append(sb, (const char *)(intptr_t)l->data[i]);
        else sb_value(sb, l->data[i], desc);
    }
    return sb->buf;
}

const char *jl_str_replace(const char *s, const char *old, const char *rep) {
    JlSb *sb = jl_sb_new();
    size_t oldlen = strlen(old);
    if (oldlen == 0) {  /* Python inserts rep around every character */
        sb_append(sb, rep);
        const char *p = s;
        while (*p) {
            const char *q = p + 1;
            while (*q && ((unsigned char)*q & 0xC0) == 0x80) q++;
            sb_append_n(sb, p, (size_t)(q - p));
            sb_append(sb, rep);
            p = q;
        }
        return sb->buf;
    }
    const char *p = s;
    for (;;) {
        const char *hit = strstr(p, old);
        if (!hit) {
            sb_append(sb, p);
            return sb->buf;
        }
        sb_append_n(sb, p, (size_t)(hit - p));
        sb_append(sb, rep);
        p = hit + oldlen;
    }
}

i64 jl_str_starts_with(const char *s, const char *prefix) { return strncmp(s, prefix, strlen(prefix)) == 0; }

i64 jl_str_ends_with(const char *s, const char *suffix) {
    size_t ls = strlen(s), lx = strlen(suffix);
    return lx <= ls && memcmp(s + ls - lx, suffix, lx) == 0;
}

/* Index (in code points) of the first occurrence of sub, or -1 */
i64 jl_str_find(const char *s, const char *sub) {
    const char *hit = strstr(s, sub);
    if (!hit) return -1;
    i64 n = 0;
    for (const char *p = s; p < hit; p++)
        if (((unsigned char)*p & 0xC0) != 0x80) n++;
    return n;
}

static uint32_t utf8_decode(const char *s, int *len) {
    const unsigned char *u = (const unsigned char *)s;
    if (u[0] < 0x80) { *len = 1; return u[0]; }
    if (u[0] < 0xE0) { *len = 2; return ((u[0] & 0x1Fu) << 6) | (u[1] & 0x3Fu); }
    if (u[0] < 0xF0) { *len = 3; return ((u[0] & 0x0Fu) << 12) | ((u[1] & 0x3Fu) << 6) | (u[2] & 0x3Fu); }
    *len = 4;
    return ((u[0] & 0x07u) << 18) | ((u[1] & 0x3Fu) << 12) | ((u[2] & 0x3Fu) << 6) | (u[3] & 0x3Fu);
}

i64 jl_str_ord(const char *s, i64 line) {
    i64 n = (i64)utf8_len(s);
    if (n != 1) {
        char msg[96];
        snprintf(msg, sizeof msg, "ord() expects a single character, found a string of length %" PRId64, n);
        jl_panic(msg, line);
    }
    int len;
    return (i64)utf8_decode(s, &len);
}

const char *jl_str_chr(i64 cp, i64 line) {
    if (cp < 0 || cp > 0x10FFFF) {
        char msg[96];
        snprintf(msg, sizeof msg, "%" PRId64 " is not a valid code point", cp);
        jl_panic(msg, line);
    }
    /* Native strings end at a zero byte, so chr(0) can't be stored */
    if (cp == 0) jl_panic("chr(0) is not supported by the native compiler", line);
    char *out = jl_alloc(5);
    uint32_t c = (uint32_t)cp;
    if (c < 0x80) { out[0] = (char)c; out[1] = 0; }
    else if (c < 0x800) { out[0] = (char)(0xC0 | (c >> 6)); out[1] = (char)(0x80 | (c & 0x3F)); out[2] = 0; }
    else if (c < 0x10000) { out[0] = (char)(0xE0 | (c >> 12)); out[1] = (char)(0x80 | ((c >> 6) & 0x3F)); out[2] = (char)(0x80 | (c & 0x3F)); out[3] = 0; }
    else { out[0] = (char)(0xF0 | (c >> 18)); out[1] = (char)(0x80 | ((c >> 12) & 0x3F)); out[2] = (char)(0x80 | ((c >> 6) & 0x3F)); out[3] = (char)(0x80 | (c & 0x3F)); out[4] = 0; }
    return out;
}

const char *jl_str_reverse(const char *s) {
    size_t n = strlen(s);
    char *out = jl_alloc(n + 1);
    size_t o = n;
    const char *p = s;
    while (*p) {
        const char *q = p + 1;
        while (*q && ((unsigned char)*q & 0xC0) == 0x80) q++;
        o -= (size_t)(q - p);
        memcpy(out + o, p, (size_t)(q - p));
        p = q;
    }
    out[n] = 0;
    return out;
}

/* Read one line from stdin without the newline ("" at end of input) */
const char *jl_input(const char *prompt) {
    fputs(prompt, stdout);
    fflush(stdout);
    JlSb *sb = jl_sb_new();
    int c;
    while ((c = getchar()) != EOF && c != '\n') sb_putc(sb, (char)c);
    if (sb->len && sb->buf[sb->len - 1] == '\r') sb->buf[--sb->len] = 0;
    return sb->buf;
}

const char *jl_str_from_int(i64 v) {
    JlSb *sb = jl_sb_new();
    jl_sb_int(sb, v);
    return sb->buf;
}

const char *jl_str_from_float(double x) {
    JlSb *sb = jl_sb_new();
    jl_sb_float(sb, x);
    return sb->buf;
}

const char *jl_str_from_bool(i64 b) { return b ? "true" : "false"; }

const char *jl_str_from_list(JlList *l, const char *desc) {
    JlSb *sb = jl_sb_new();
    jl_sb_list(sb, l, desc);
    return sb->buf;
}

static char *clean_number(const char *s) {
    while (*s && isspace((unsigned char)*s)) s++;
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1])) n--;
    char *out = jl_alloc(n + 1);
    size_t k = 0;
    for (size_t i = 0; i < n; i++)
        if (s[i] != '_') out[k++] = s[i];
    out[k] = 0;
    return out;
}

i64 jl_str_to_int(const char *s, i64 line) {
    char *clean = clean_number(s);
    char *end;
    if (!*clean) goto bad;
    errno = 0;
    long long v = strtoll(clean, &end, 10);
    if (*end) goto bad;
    if (errno == ERANGE) jl_panic_overflow(line);
    return (i64)v;
bad: {
        JlSb *sb = jl_sb_new();
        sb_append(sb, "cannot parse ");
        sb_quoted(sb, s);
        sb_append(sb, " as an integer");
        jl_panic(sb->buf, line);
    }
}

double jl_str_to_float(const char *s, i64 line) {
    char *clean = clean_number(s);
    char *end;
    if (!*clean) goto bad;
    double v = strtod(clean, &end);
    if (*end) goto bad;
    return v;
bad: {
        JlSb *sb = jl_sb_new();
        sb_append(sb, "cannot parse ");
        sb_quoted(sb, s);
        sb_append(sb, " as a number");
        jl_panic(sb->buf, line);
    }
}

/* Lists (each element is 8 bytes: an int, bool, pointer, or double bits) */

JlList *jl_list_new(i64 cap) {
    JlList *l = jl_alloc(sizeof *l);
    l->len = 0;
    l->cap = cap > 4 ? cap : 4;
    l->data = jl_alloc(sizeof(i64) * (size_t)l->cap);
    return l;
}

JlList *jl_list_push(JlList *l, i64 bits) {
    if (l->len == l->cap) {
        l->cap *= 2;
        l->data = jl_realloc(l->data, sizeof(i64) * (size_t)l->cap);
    }
    l->data[l->len++] = bits;
    return l;
}

i64 jl_list_pop(JlList *l, i64 line) {
    if (l->len == 0) jl_panic("pop() from an empty list", line);
    return l->data[--l->len];
}

NORETURN void jl_panic_index(JlList *l, i64 index, i64 line) {
    char msg[160];
    snprintf(msg, sizeof msg, "index %" PRId64 " is out of range for list of length %" PRId64, index, l->len);
    jl_panic(msg, line);
}

JlList *jl_list_repeat(JlList *l, i64 n, i64 line) {
    check_repeat(l->len, n, line);
    i64 total = n > 0 ? l->len * n : 0;
    JlList *out = jl_list_new(total);
    for (i64 k = 0; k < n; k++)
        for (i64 i = 0; i < l->len; i++) out->data[out->len++] = l->data[i];
    return out;
}

JlList *jl_list_concat(JlList *a, JlList *b) {
    JlList *out = jl_list_new(a->len + b->len);
    memcpy(out->data, a->data, sizeof(i64) * (size_t)a->len);
    memcpy(out->data + a->len, b->data, sizeof(i64) * (size_t)b->len);
    out->len = a->len + b->len;
    return out;
}

i64 jl_list_contains_int(JlList *l, i64 v) {
    for (i64 i = 0; i < l->len; i++)
        if (l->data[i] == v) return 1;
    return 0;
}

i64 jl_list_contains_float(JlList *l, double v) {
    for (i64 i = 0; i < l->len; i++) {
        double d;
        memcpy(&d, &l->data[i], 8);
        if (d == v) return 1;
    }
    return 0;
}

i64 jl_list_contains_float_in_int(JlList *l, double v) {
    for (i64 i = 0; i < l->len; i++)
        if ((double)l->data[i] == v) return 1;
    return 0;
}

i64 jl_list_contains_str(JlList *l, const char *s) {
    for (i64 i = 0; i < l->len; i++)
        if (strcmp((const char *)(intptr_t)l->data[i], s) == 0) return 1;
    return 0;
}

static int cmp_int(const void *a, const void *b) {
    i64 x = *(const i64 *)a, y = *(const i64 *)b;
    return (x > y) - (x < y);
}

static int cmp_float(const void *a, const void *b) {
    double x, y;
    memcpy(&x, a, 8);
    memcpy(&y, b, 8);
    return (x > y) - (x < y);
}

static int cmp_str(const void *a, const void *b) {
    return strcmp((const char *)(intptr_t)*(const i64 *)a, (const char *)(intptr_t)*(const i64 *)b);
}

/* Sorted copy of a list ('i' ints and bools, 'f' floats, 's' strings) */
JlList *jl_list_sorted(JlList *l, const char *desc) {
    JlList *out = jl_list_concat(l, jl_list_new(0));
    int (*cmp)(const void *, const void *) = desc[0] == 'f' ? cmp_float : desc[0] == 's' ? cmp_str : cmp_int;
    qsort(out->data, (size_t)out->len, sizeof(i64), cmp);
    return out;
}

JlList *jl_list_reversed(JlList *l) {
    JlList *out = jl_list_new(l->len);
    for (i64 i = l->len - 1; i >= 0; i--) out->data[out->len++] = l->data[i];
    return out;
}

i64 jl_list_index_int(JlList *l, i64 v) {
    for (i64 i = 0; i < l->len; i++)
        if (l->data[i] == v) return i;
    return -1;
}

i64 jl_list_index_float(JlList *l, double v) {
    for (i64 i = 0; i < l->len; i++) {
        double d;
        memcpy(&d, &l->data[i], 8);
        if (d == v) return i;
    }
    return -1;
}

i64 jl_list_index_float_in_int(JlList *l, double v) {
    for (i64 i = 0; i < l->len; i++)
        if ((double)l->data[i] == v) return i;
    return -1;
}

i64 jl_list_index_str(JlList *l, const char *s) {
    for (i64 i = 0; i < l->len; i++)
        if (strcmp((const char *)(intptr_t)l->data[i], s) == 0) return i;
    return -1;
}

i64 jl_list_sum_int(JlList *l, i64 line) {
    i64 total = 0;
    for (i64 i = 0; i < l->len; i++)
        if (__builtin_add_overflow(total, l->data[i], &total)) jl_panic_overflow(line);
    return total;
}

double jl_list_sum_float(JlList *l) {
    double total = 0;
    for (i64 i = 0; i < l->len; i++) {
        double d;
        memcpy(&d, &l->data[i], 8);
        total += d;
    }
    return total;
}

i64 jl_list_product_int(JlList *l, i64 line) {
    i64 total = 1;
    for (i64 i = 0; i < l->len; i++)
        if (__builtin_mul_overflow(total, l->data[i], &total)) jl_panic_overflow(line);
    return total;
}

double jl_list_product_float(JlList *l) {
    double total = 1;
    for (i64 i = 0; i < l->len; i++) {
        double d;
        memcpy(&d, &l->data[i], 8);
        total *= d;
    }
    return total;
}

i64 jl_list_minmax_int(JlList *l, i64 want_max, i64 line) {
    if (l->len == 0) jl_panic(want_max ? "max() of an empty sequence" : "min() of an empty sequence", line);
    i64 best = l->data[0];
    for (i64 i = 1; i < l->len; i++) {
        i64 v = l->data[i];
        if (want_max ? v > best : v < best) best = v;
    }
    return best;
}

double jl_list_minmax_float(JlList *l, i64 want_max, i64 line) {
    if (l->len == 0) jl_panic(want_max ? "max() of an empty sequence" : "min() of an empty sequence", line);
    double best;
    memcpy(&best, &l->data[0], 8);
    for (i64 i = 1; i < l->len; i++) {
        double v;
        memcpy(&v, &l->data[i], 8);
        if (want_max ? v > best : v < best) best = v;
    }
    return best;
}

/* Math */

/* Use the same UCRT math functions as CPython on Windows, so the printed digits match */
#ifdef _WIN32
typedef double (*jl_fn1)(double);
typedef double (*jl_fn2)(double, double);
static void *jl_ucrt_fn(const char *name, void *fallback) {
    static HMODULE ucrt;
    static int tried;
    if (!tried) {
        tried = 1;
        ucrt = LoadLibraryA("ucrtbase.dll");
    }
    void *f = ucrt ? (void *)GetProcAddress(ucrt, name) : NULL;
    return f ? f : fallback;
}
#define JL_M1(name) \
    static double m_##name(double x) { \
        static jl_fn1 f; \
        if (!f) f = (jl_fn1)jl_ucrt_fn(#name, (void *)name); \
        return f(x); \
    }
#define JL_M2(name) \
    static double m_##name(double x, double y) { \
        static jl_fn2 f; \
        if (!f) f = (jl_fn2)jl_ucrt_fn(#name, (void *)name); \
        return f(x, y); \
    }
#else
#define JL_M1(name)     static double m_##name(double x) { return name(x); }
#define JL_M2(name)     static double m_##name(double x, double y) { return name(x, y); }
#endif
JL_M1(sin) JL_M1(cos) JL_M1(tan) JL_M1(asin) JL_M1(acos) JL_M1(atan) JL_M1(sinh) JL_M1(cosh) JL_M1(tanh)
JL_M1(exp) JL_M1(log) JL_M1(log2) JL_M1(log10) JL_M1(cbrt)
JL_M2(pow) JL_M2(atan2) JL_M2(hypot) JL_M2(fmod)

double jl_atan2(double y, double x) { return m_atan2(y, x); }

double jl_hypot(double x, double y) { return m_hypot(x, y); }

/* Same order as MATH_OPS in codegen.py */
static const char *MATH_NAMES[] = {"sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
                                   "exp", "ln", "log2", "log10", "cbrt", "deg", "rad", "log"};

/* Show a number like the interpreter's error messages do, so an int argument has no .0 */
static void show_number(double x, i64 is_int, char *out) {
    if (is_int) sprintf(out, "%.0f", x);
    else jl_repr_double(x, out);
}

NORETURN static void math_undefined(i64 op, double x, i64 is_int, i64 line) {
    char num[48], msg[128];
    show_number(x, is_int, num);
    snprintf(msg, sizeof msg, "%s(%s) is undefined", MATH_NAMES[op], num);
    jl_panic(msg, line);
}

/* Unary math with Python's errors: NaN from a non-NaN input is a domain error,
 * infinity from a finite input is a range error */
double jl_math1(i64 op, double x, i64 is_int, i64 line) {
    double r;
    switch (op) {
        case 0: r = m_sin(x); break;
        case 1: r = m_cos(x); break;
        case 2: r = m_tan(x); break;
        case 3: r = m_asin(x); break;
        case 4: r = m_acos(x); break;
        case 5: r = m_atan(x); break;
        case 6: r = m_sinh(x); break;
        case 7: r = m_cosh(x); break;
        case 8: r = m_tanh(x); break;
        case 9: r = m_exp(x); break;
        case 10: case 16: if (x <= 0) math_undefined(op, x, is_int, line); r = m_log(x); break;
        case 11: if (x <= 0) math_undefined(op, x, is_int, line); r = m_log2(x); break;
        case 12: if (x <= 0) math_undefined(op, x, is_int, line); r = m_log10(x); break;
        case 13: r = m_cbrt(x); break;
        case 14: return x * (180.0 / 3.14159265358979323846);
        case 15: return x * (3.14159265358979323846 / 180.0);
        default: r = x;
    }
    if (isnan(r) && !isnan(x)) math_undefined(op, x, is_int, line);
    if (isinf(r) && isfinite(x)) math_undefined(op, x, is_int, line);
    return r;
}

NORETURN void jl_panic_sqrt(double x, i64 is_int, i64 line) {
    char num[48], msg[128];
    show_number(x, is_int, num);
    snprintf(msg, sizeof msg, "square root of a negative number (%s)", num);
    jl_panic(msg, line);
}

/* int_args: bit 0 is set when x is an int, bit 1 when base is */
double jl_log_base(double x, double base, i64 int_args, i64 line) {
    if (x <= 0) math_undefined(16, x, int_args & 1, line);
    if (base <= 0 || base == 1) {
        char num[48], msg[96];
        show_number(base, int_args & 2, num);
        snprintf(msg, sizeof msg, "invalid logarithm base %s", num);
        jl_panic(msg, line);
    }
    return m_log(x) / m_log(base);
}

i64 jl_ipow(i64 base, i64 exp, i64 line) {
    if (exp < 0) {
        if (base == 0) jl_panic("0 cannot be raised to a negative power", line);
        jl_panic("negative exponent in integer power (native code needs a float base, e.g. 2.0^n)", line);
    }
    i64 result = 1;
    while (exp) {
        if (exp & 1)
            if (__builtin_mul_overflow(result, base, &result)) jl_panic_overflow(line);
        exp >>= 1;
        if (exp && __builtin_mul_overflow(base, base, &base)) {
            /* base overflowed but might not be needed (e.g. 1^n or 0^n) */
            if (result != 0) jl_panic_overflow(line);
            return 0;
        }
    }
    return result;
}

double jl_fpow(double a, double b, i64 line) {
    if (a == 0 && b < 0) jl_panic("0 cannot be raised to a negative power", line);
    if (a < 0 && b != floor(b) && isfinite(b)) jl_panic("cannot raise a negative number to a fractional power", line);
    double r = m_pow(a, b);
    if (isinf(r) && isfinite(a) && isfinite(b)) jl_panic("numeric overflow in `^`", line);
    return r;
}

/* Float floor division and modulo, same as CPython's float_divmod */
static void py_divmod(double vx, double wx, double *floordiv, double *modulo) {
    double mod = m_fmod(vx, wx);
    double div = (vx - mod) / wx;
    if (mod) {
        if ((wx < 0) != (mod < 0)) {
            mod += wx;
            div -= 1.0;
        }
    } else {
        mod = copysign(0.0, wx);
    }
    if (div) {
        double fd = floor(div);
        if (div - fd > 0.5) fd += 1.0;
        div = fd;
    } else {
        div = copysign(0.0, vx / wx);
    }
    *floordiv = div;
    *modulo = mod;
}

double jl_ffloordiv(double a, double b, i64 line) {
    if (b == 0) jl_panic_divzero(line);
    double d, m;
    py_divmod(a, b, &d, &m);
    return d;
}

double jl_fmod(double a, double b, i64 line) {
    if (b == 0) jl_panic_modzero(line);
    double d, m;
    py_divmod(a, b, &d, &m);
    return m;
}

i64 jl_ifact(i64 n, i64 line) {
    if (n < 0) {
        char msg[96];
        snprintf(msg, sizeof msg, "factorial of a negative number (%" PRId64 ")", n);
        jl_panic(msg, line);
    }
    i64 r = 1;
    for (i64 i = 2; i <= n; i++)
        if (__builtin_mul_overflow(r, i, &r)) jl_panic_overflow(line);
    return r;
}

double jl_ffact(double x, i64 line) {
    if (x == floor(x) && x >= 0) {
        if (x > 170) return INFINITY;
        /* long double has enough bits that this rounds like float(math.factorial(n)) for every n <= 170 */
        long double r = 1;
        for (int i = 2; i <= (int)x; i++) r *= i;
        return (double)r;
    }
    double r = tgamma(x + 1);
    if (isnan(r) || isinf(r)) {
        char num[48], msg[96];
        jl_repr_double(x, num);
        snprintf(msg, sizeof msg, "factorial is undefined for %s", num);
        jl_panic(msg, line);
    }
    return r;
}

/* mode: 0 int(), 1 floor, 2 ceil, 3 round half away from zero, 4 trunc */
i64 jl_float_to_int(double x, i64 mode, i64 line) {
    if (isnan(x) || isinf(x)) {
        static const char *const errors[] = {"cannot convert %s to int", "cannot take floor of %s",
                                             "cannot take ceil of %s", "cannot round %s", "cannot truncate %s"};
        char num[48], msg[96];
        jl_repr_double(x, num);
        snprintf(msg, sizeof msg, errors[mode], num);
        jl_panic(msg, line);
    }
    double r;
    switch (mode) {
        case 1: r = floor(x); break;
        case 2: r = ceil(x); break;
        case 3: r = round(x); break;
        default: r = trunc(x); break;
    }
    if (r >= 9223372036854775808.0 || r < -9223372036854775808.0) jl_panic_overflow(line);
    return (i64)r;
}

/* Round half up on the shortest decimal repr, like the interpreter (which uses Decimal) */
double jl_round_digits(double x, i64 nd) {
    if (isnan(x) || isinf(x) || x == 0) return x;
    char digits[40];
    int decpt;
    int neg = x < 0;
    int n = jl_shortest_digits(fabs(x), digits, &decpt);
    if (nd > 400) return x;
    if (nd < -400) nd = -400; /* rounds to 0 either way, and decpt + nd can't overflow */
    i64 keep = decpt + nd; /* number of significant digits to keep */
    if (keep >= n) return x;
    char buf[80];
    if (keep < 0) return neg ? -0.0 : 0.0;
    int carry = digits[keep] >= '5';
    char kept[40];
    memcpy(kept, digits, (size_t)keep);
    kept[keep] = 0;
    if (carry) {
        i64 i = keep - 1;
        while (i >= 0) {
            if (kept[i] == '9') {
                kept[i] = '0';
                i--;
            } else {
                kept[i]++;
                break;
            }
        }
        if (i < 0) {
            memmove(kept + 1, kept, (size_t)keep + 1);
            kept[0] = '1';
            decpt++;
            keep++;
        }
    }
    if (keep == 0) return neg ? -0.0 : 0.0;
    snprintf(buf, sizeof buf, "%s0.%se%d", neg ? "-" : "", kept, decpt);
    return strtod(buf, NULL);
}

i64 jl_round_int_digits(i64 x, i64 nd, i64 line) {
    if (nd >= 0) return x;
    if (nd < -18) {
        /* 10^19 doesn't fit, so only values of at least 5 * 10^18 round away from 0, and they overflow */
        if (nd == -19 && (x >= 5000000000000000000LL || x <= -5000000000000000000LL)) jl_panic_overflow(line);
        return 0;
    }
    i64 p = 1;
    for (i64 i = 0; i < -nd; i++) p *= 10;
    i64 q = x / p, r = x % p;
    i64 ar = r < 0 ? -r : r;
    if (ar * 2 >= p) q += (x < 0 ? -1 : 1);
    i64 out;
    if (__builtin_mul_overflow(q, p, &out)) jl_panic_overflow(line);
    return out;
}

static uint64_t abs_u64(i64 v) { return v < 0 ? (uint64_t)0 - (uint64_t)v : (uint64_t)v; }

static uint64_t gcd_u64(uint64_t x, uint64_t y) {
    while (y) {
        uint64_t t = x % y;
        x = y;
        y = t;
    }
    return x;
}

/* The results are worked out unsigned, since gcd(-2^63, 0) is 2^63 which doesn't fit */
i64 jl_gcd(i64 a, i64 b, i64 line) {
    uint64_t g = gcd_u64(abs_u64(a), abs_u64(b));
    if (g > INT64_MAX) jl_panic_overflow(line);
    return (i64)g;
}

i64 jl_lcm(i64 a, i64 b, i64 line) {
    if (a == 0 || b == 0) return 0;
    uint64_t x = abs_u64(a), y = abs_u64(b), r;
    if (__builtin_mul_overflow(x / gcd_u64(x, y), y, &r) || r > INT64_MAX) jl_panic_overflow(line);
    return (i64)r;
}

i64 jl_isqrt(i64 n, i64 line) {
    if (n < 0) {
        char msg[96];
        snprintf(msg, sizeof msg, "isqrt of a negative number (%" PRId64 ")", n);
        jl_panic(msg, line);
    }
    if (n < 2) return n;
    uint64_t x = (uint64_t)sqrt((double)n);
    while (x * x > (uint64_t)n) x--;
    while ((x + 1) * (x + 1) <= (uint64_t)n) x++;
    return (i64)x;
}

static uint64_t mulmod(uint64_t a, uint64_t b, uint64_t m) { return (uint64_t)((unsigned __int128)a * b % m); }

static uint64_t powmod(uint64_t a, uint64_t e, uint64_t m) {
    uint64_t r = 1;
    a %= m;
    while (e) {
        if (e & 1) r = mulmod(r, a, m);
        a = mulmod(a, a, m);
        e >>= 1;
    }
    return r;
}

i64 jl_is_prime(i64 sn) {
    if (sn < 2) return 0;
    uint64_t n = (uint64_t)sn;
    static const uint64_t small[] = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37};
    for (int i = 0; i < 12; i++) {
        if (n % small[i] == 0) return n == small[i];
    }
    uint64_t d = n - 1;
    int s = 0;
    while ((d & 1) == 0) {
        d >>= 1;
        s++;
    }
    for (int i = 0; i < 12; i++) {
        uint64_t x = powmod(small[i], d, n);
        if (x == 1 || x == n - 1) continue;
        int composite = 1;
        for (int r = 1; r < s; r++) {
            x = mulmod(x, x, n);
            if (x == n - 1) {
                composite = 0;
                break;
            }
        }
        if (composite) return 0;
    }
    return 1;
}

double jl_clock(void) {
#ifdef _WIN32
    static LARGE_INTEGER freq;
    LARGE_INTEGER now;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ts.tv_nsec / 1e9;
#endif
}

void jl_exit(i64 code) {
    fflush(stdout);
    exit((int)code);
}

/* Entry point */

static char jl_stdout_buffer[1 << 16];

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
#ifdef _WIN32
    SetConsoleOutputCP(65001);
#endif
    /* Buffer output when it goes to a pipe or file, but not on the console */
    if (!JL_ISATTY(JL_FILENO(stdout))) setvbuf(stdout, jl_stdout_buffer, _IOFBF, sizeof jl_stdout_buffer);
    jl_main();
    fflush(stdout);
    return 0;
}
