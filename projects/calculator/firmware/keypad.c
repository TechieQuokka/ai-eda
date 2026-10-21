// Matrix scan without diodes: idle rows are high-impedance inputs, the scanned row an
// output driving low, so two pressed keys never connect a high output to a low one.
#include "keypad.h"

#include "pico/stdlib.h"
#include "pins.h"

#define ROWS 5
#define COLUMNS 4

static const unsigned row_pins[ROWS] = {PIN_ROW1, PIN_ROW2, PIN_ROW3, PIN_ROW4, PIN_ROW5};
static const unsigned column_pins[COLUMNS] = {PIN_COL1, PIN_COL2, PIN_COL3, PIN_COL4};

// Key legends as calc.h codes, in board order (row 1 at the top).
static const char keys[ROWS][COLUMNS] = {
    {'c', 'b', '%', '/'},
    {'7', '8', '9', '*'},
    {'4', '5', '6', '-'},
    {'1', '2', '3', '+'},
    {'n', '0', '.', '='},
};

static char candidate;  // key seen on the last scans
static unsigned seen;   // scans in a row it was seen
static char reported;   // key last reported (0: released)

void keypad_init(void) {
    for (unsigned r = 0; r < ROWS; r++) gpio_put(row_pins[r], 0);  // low once driven
}

static char scan(void) {
    char found = 0;
    for (unsigned r = 0; r < ROWS; r++) {
        gpio_set_dir(row_pins[r], GPIO_OUT);
        sleep_us(10);  // let the column lines settle (pull-up against wiring capacitance)
        for (unsigned c = 0; c < COLUMNS; c++) {
            if (!found && !gpio_get(column_pins[c])) found = keys[r][c];
        }
        gpio_set_dir(row_pins[r], GPIO_IN);
    }
    return found;
}

char keypad_poll(void) {
    char key = scan();
    if (key != candidate) {
        candidate = key;
        seen = 0;
    }
    if (seen < KEYPAD_DEBOUNCE) seen++;
    if (seen < KEYPAD_DEBOUNCE || candidate == reported) return 0;
    reported = candidate;
    return candidate;
}
