// HD44780 character LCD, 4-bit bus (D4-D7, RS, E), write only (RW tied to GND).
#ifndef LCD_H
#define LCD_H

#include <stdint.h>

void lcd_init(void);                          // power-up wait and init by instruction
void lcd_clear(void);
void lcd_goto(unsigned row, unsigned column);  // row 0 or 1
void lcd_print(const char *text);
void lcd_print_line(unsigned row, const char *text);  // whole line, padded with spaces
void lcd_define_char(unsigned code, const uint8_t rows[8]);  // CGRAM character 0-7

#endif
