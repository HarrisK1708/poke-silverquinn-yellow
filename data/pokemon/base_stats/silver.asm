	db DEX_SILVER ; pokedex id

	db 115, 130, 140,  60, 135
	;   hp  atk  def  spd  spc

	db GROUND, GROUND ; type
	db 45 ; catch rate
	db 215 ; base exp

	; TODO(art): placeholder pics, see gfx/pics.asm "Pics 8"
	INCBIN "gfx/pokemon/front/silver.pic", 0, 1 ; sprite dimensions
	dw SilverPicFront, SilverPicBack

	db TACKLE, HARDEN, SAND_ATTACK, NO_MOVE ; level 1 learnset
	db GROWTH_MEDIUM_FAST ; growth rate

	; tm/hm learnset
	tmhm MEGA_PUNCH,   MEGA_KICK,    TOXIC,        BODY_SLAM,    TAKE_DOWN,    \
	     DOUBLE_EDGE,  HYPER_BEAM,   SUBMISSION,   COUNTER,      SEISMIC_TOSS, \
	     RAGE,         EARTHQUAKE,   FISSURE,      DIG,          MIMIC,        \
	     DOUBLE_TEAM,  REFLECT,      BIDE,         SKULL_BASH,   REST,         \
	     ROCK_SLIDE,   SUBSTITUTE,   STRENGTH
	; end

	db 0 ; padding
