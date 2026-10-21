// 5 x 4 key matrix: rows driven low one at a time, columns read with pull-ups.
#ifndef KEYPAD_H
#define KEYPAD_H

void keypad_init(void);

// Scans the matrix once; call every KEYPAD_SCAN_MS. Returns the key code (see calc.h)
// once when a key has read pressed on KEYPAD_DEBOUNCE scans in a row, otherwise 0.
char keypad_poll(void);

#define KEYPAD_SCAN_MS 5
#define KEYPAD_DEBOUNCE 3

#endif
