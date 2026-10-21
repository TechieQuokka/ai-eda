// Immediate-execution calculator: each operator applies to the value so far and the
// number typed after it. The state is the committed part of the expression (`prefix`,
// its value `acc`, the pending operator `op`) and the number being typed (`entry`).
#include "calc.h"

#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_DIGITS 9      // digits in one number (the result shows 9 significant digits)
#define PREFIX_SIZE 128   // committed expression text
#define HISTORY 16        // operators that backspace can undo
#define LIMIT 1e100       // results this large count as an overflow

typedef enum { TYPING, RESULT, FAILED } Mode;

typedef struct {
    char prefix[PREFIX_SIZE];  // e.g. "12+3*"
    double acc;                // value of the prefix
    char op;                   // pending operator, 0 before the first one
    char entry[MAX_DIGITS + 4];  // number being typed: optional '-', digits, '.', '%'
    double percent;            // the entry's value once '%' is applied
} State;

static State state;
static State history[HISTORY];  // state before each operator, for backspace
static unsigned depth;
static Mode mode;
static double result;
static char lines[2][CALC_COLUMNS + 1];

static bool has_percent(void) { return strchr(state.entry, '%') != NULL; }

static unsigned digit_count(void) {
    unsigned count = 0;
    for (const char *c = state.entry; *c; c++) count += (*c >= '0' && *c <= '9');
    return count;
}

static double entry_value(void) {
    return has_percent() ? state.percent : strtod(state.entry, NULL);
}

static bool entry_empty(void) { return state.entry[0] == '\0' || strcmp(state.entry, "-") == 0; }

// Applies op to a and b; false on a division by zero or an overflow.
static bool apply(char op, double a, double b, double *out) {
    double value;
    switch (op) {
        case '+': value = a + b; break;
        case '-': value = a - b; break;
        case '*': value = a * b; break;
        case '/':
            if (b == 0.0) return false;
            value = a / b;
            break;
        default: value = b; break;  // no operator yet: the number itself
    }
    if (!isfinite(value) || fabs(value) >= LIMIT) return false;
    *out = value;
    return true;
}

// At most 15 characters, like printf's "%.9g": 9 significant digits without trailing
// zeros, plain for 1e-5 <= |value| < 1e9 when that fits, else "1.52415788e+16". Written
// out by hand because the pico-sdk's printf keeps the trailing zeros of %g.
static void format_number(double value, char *text, size_t size) {
    double magnitude = fabs(value);
    if (magnitude < 1e-99) {
        snprintf(text, size, "0");
        return;
    }
    int exponent = (int)floor(log10(magnitude));
    unsigned long mantissa = (unsigned long)llround(magnitude / pow(10.0, exponent - 8));
    if (mantissa >= 1000000000ul) {  // rounding carried over (9.999999999 -> 10)
        mantissa = (mantissa + 5) / 10;
        exponent++;
    } else if (mantissa < 100000000ul) {  // log10 rounded up just below a power of ten
        mantissa *= 10;
        exponent--;
    }
    char digits[24];
    snprintf(digits, sizeof digits, "%09lu", mantissa);
    int count = 9;
    while (count > 1 && digits[count - 1] == '0') count--;

    char plain[32];
    int length = 0;
    if (value < 0) plain[length++] = '-';
    if (exponent >= 0) {
        for (int i = 0; i <= exponent; i++) plain[length++] = i < count ? digits[i] : '0';
        if (count > exponent + 1) {
            plain[length++] = '.';
            for (int i = exponent + 1; i < count; i++) plain[length++] = digits[i];
        }
    } else {
        plain[length++] = '0';
        plain[length++] = '.';
        for (int i = 0; i < -exponent - 1; i++) plain[length++] = '0';
        for (int i = 0; i < count; i++) plain[length++] = digits[i];
    }
    plain[length] = '\0';
    if (exponent >= -5 && exponent < 9 && length <= CALC_COLUMNS - 1) {
        snprintf(text, size, "%s", plain);
        return;
    }
    char fraction[10] = "";
    if (count > 1) {
        fraction[0] = '.';
        memcpy(fraction + 1, digits + 1, (size_t)count - 1);
        fraction[count] = '\0';
    }
    snprintf(text, size, "%s%c%se%c%02d", value < 0 ? "-" : "", digits[0], fraction,
             exponent < 0 ? '-' : '+', exponent < 0 ? -exponent : exponent);
}

static void append(char *text, size_t size, const char *tail) {
    size_t length = strlen(text);
    snprintf(text + length, size - length, "%s", tail);
}

static void render(void) {
    char expression[PREFIX_SIZE + sizeof state.entry + 2];
    snprintf(expression, sizeof expression, "%s%s%s", state.prefix, state.entry,
             mode == RESULT ? "=" : "");
    size_t length = strlen(expression);
    size_t start = length > CALC_COLUMNS ? length - CALC_COLUMNS : 0;
    memcpy(lines[0], expression + start, length - start + 1);

    char value[24] = "";
    double shown;
    if (mode == FAILED) {
        snprintf(value, sizeof value, "Error");
    } else if (mode == RESULT) {
        format_number(result, value, sizeof value);
    } else if (entry_empty()) {
        format_number(state.op ? state.acc : 0.0, value, sizeof value);
    } else if (apply(state.op, state.acc, entry_value(), &shown)) {
        format_number(shown, value, sizeof value);
    }  // else: a division by zero not finished yet shows nothing until '='
    snprintf(lines[1], sizeof lines[1], "%*.15s", CALC_COLUMNS, value);
}

void calc_reset(void) {
    memset(&state, 0, sizeof state);
    depth = 0;
    mode = TYPING;
    result = 0.0;
    render();
}

static void type_char(char key) {
    if (mode != TYPING) calc_reset();
    if (has_percent()) return;
    size_t length = strlen(state.entry);
    if (key == '.') {
        if (strchr(state.entry, '.')) return;
        if (entry_empty()) {
            append(state.entry, sizeof state.entry, "0.");
            return;
        }
    } else {
        if (digit_count() >= MAX_DIGITS) return;
        const char *number = state.entry[0] == '-' ? state.entry + 1 : state.entry;
        if (strcmp(number, "0") == 0) length--;  // a leading zero gives way to the digit
    }
    state.entry[length] = key;
    state.entry[length + 1] = '\0';
}

static void press_operator(char key) {
    if (mode == FAILED) return;
    if (mode == RESULT) {
        double value = result;
        calc_reset();
        format_number(value, state.prefix, sizeof state.prefix);
        state.acc = value;
    } else if (entry_empty()) {
        size_t length = strlen(state.prefix);
        if (length > 0) {  // replace the operator just typed
            state.prefix[length - 1] = key;
            state.op = key;
            return;
        }
        snprintf(state.prefix, sizeof state.prefix, "0");  // an operator first: 0 op ...
        state.acc = 0.0;
        state.entry[0] = '\0';
    } else {
        if (depth < HISTORY) history[depth++] = state;
        double value;
        if (!apply(state.op, state.acc, entry_value(), &value)) {
            mode = FAILED;
            return;
        }
        append(state.prefix, sizeof state.prefix, state.entry);
        state.entry[0] = '\0';
        state.acc = value;
    }
    char text[2] = {key, '\0'};
    append(state.prefix, sizeof state.prefix, text);
    state.op = key;
}

static void equals(void) {
    if (mode != TYPING) return;
    if (entry_empty()) {
        size_t length = strlen(state.prefix);
        if (length > 0) state.prefix[length - 1] = '\0';  // a trailing operator is dropped
        state.entry[0] = '\0';
        result = state.acc;
        mode = RESULT;
        return;
    }
    if (!apply(state.op, state.acc, entry_value(), &result)) {
        mode = FAILED;
        return;
    }
    mode = RESULT;
}

static void backspace(void) {
    if (mode == FAILED) {
        calc_reset();
        return;
    }
    if (mode == RESULT) return;
    size_t length = strlen(state.entry);
    if (length > 0) {
        state.entry[length - 1] = '\0';
        if (strcmp(state.entry, "-") == 0) state.entry[0] = '\0';
    } else if (depth > 0) {
        state = history[--depth];  // undo the last operator: its number is editable again
    }
}

static void change_sign(void) {
    if (mode == FAILED) return;
    if (mode == RESULT) {
        char text[24];
        format_number(-result, text, sizeof text);
        calc_reset();
        if (!strchr(text, 'e') && strlen(text) < sizeof state.entry) {
            memcpy(state.entry, text, strlen(text) + 1);
        }  // else: the exponent form cannot be edited as a number; start over
        return;
    }
    if (has_percent()) return;
    size_t length = strlen(state.entry);
    if (state.entry[0] == '-') {
        memmove(state.entry, state.entry + 1, length);
    } else if (length + 1 < sizeof state.entry) {
        memmove(state.entry + 1, state.entry, length + 1);
        state.entry[0] = '-';
    }
}

// x% is x/100, except after + or -, where it is that share of the value so far
// (200+10% = 220, 200-10% = 180).
static void percent(void) {
    if (mode != TYPING || entry_empty() || has_percent()) return;
    double value = strtod(state.entry, NULL) / 100.0;
    if (state.op == '+' || state.op == '-') value *= state.acc;
    state.percent = value;
    append(state.entry, sizeof state.entry, "%");
}

void calc_key(char key) {
    if ((key >= '0' && key <= '9') || key == '.') {
        type_char(key);
    } else if (key == '+' || key == '-' || key == '*' || key == '/') {
        press_operator(key);
    } else if (key == '=') {
        equals();
    } else if (key == 'b') {
        backspace();
    } else if (key == 'n') {
        change_sign();
    } else if (key == '%') {
        percent();
    } else if (key == 'c') {
        calc_reset();
        return;
    } else {
        return;
    }
    render();
}

const char *calc_line(unsigned row) { return lines[row ? 1 : 0]; }
