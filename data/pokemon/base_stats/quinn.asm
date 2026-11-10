	db DEX_QUINN ; pokedex id

	db 100,  95, 100, 105, 180
	;   hp  atk  def  spd  spc

	db GRASS, GRASS ; type
	db 45 ; catch rate
	db 216 ; base exp

	; TODO(art): placeholder pics, see gfx/pics.asm "Pics 8"
	INCBIN "gfx/pokemon/front/quinn.pic", 0, 1 ; sprite dimensions
	dw QuinnPicFront, QuinnPicBack

	db ABSORB, GROWTH, NO_MOVE, NO_MOVE ; level 1 learnset
	db GROWTH_MEDIUM_FAST ; growth rate

	; tm/hm learnset
	tmhm SWORDS_DANCE, TOXIC,        BODY_SLAM,    TAKE_DOWN,    DOUBLE_EDGE,  \
	     HYPER_BEAM,   RAGE,         MEGA_DRAIN,   SOLARBEAM,    MIMIC,        \
	     DOUBLE_TEAM,  REFLECT,      BIDE,         REST,         SUBSTITUTE,   \
	     CUT,          FLASH
	; end

	db 0 ; padding
