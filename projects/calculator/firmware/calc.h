// Calculator logic, independent of the hardware: keys in, two text lines out.
//
// Keys: '0'-'9', '.', '+', '-', '*', '/', '=', '%' (percent), 'n' (change sign),
// 'b' (backspace), 'c' (clear). Operations run in the order they are typed, as on a
// pocket calculator: 2+3*4= gives 20.
#ifndef CALC_H
#define CALC_H

#define CALC_COLUMNS 16

void calc_reset(void);
void calc_key(char key);

// Line 0: the expression as typed (its last 16 characters), "=" after a result.
// Line 1: the value so far, right-aligned in 15 characters (the first column is left
// free for the low-battery mark); "Error" after a division by zero or an overflow.
// Operators are the ASCII characters above; the display maps them to its own glyphs.
const char *calc_line(unsigned row);

#endif
