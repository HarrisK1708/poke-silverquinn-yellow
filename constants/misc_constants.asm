; Boolean checks
DEF FALSE EQU 0
DEF TRUE  EQU 1

; flag operations
	const_def
	const FLAG_RESET ; 0
	const FLAG_SET   ; 1
	const FLAG_TEST  ; 2

; input
DEF NO_INPUT EQU 0

; SGB command MLT_REQ can be used to detect SGB hardware
DEF JOYP_SGB_MLT_REQ EQU %00000011

; save-format version: the running total CalcGameDataCheckSum starts from (see
; engine/menus/save.asm). The main save checksum is a plain byte sum, so a save
; written with any other salt (vanilla and pre-#152 builds used 0) never matches
; and is rejected as "file data destroyed" instead of loading with shifted data.
; Bump this whenever the saved layout (sGameData) changes. Must be nonzero mod 256.
DEF SAVE_FORMAT_SALT EQU $01 ; format version 1: Pokédex flags 151 -> 154 bits
	ASSERT SAVE_FORMAT_SALT % $100 != 0
