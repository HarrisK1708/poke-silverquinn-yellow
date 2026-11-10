	db DEX_RAYA ; pokedex id

	db 110, 140, 100, 110, 120
	;   hp  atk  def  spd  spc

	db DRAGON, DRAGON ; type
	db 45 ; catch rate
	db 217 ; base exp

	; TODO(art): placeholder pics, see gfx/pics.asm "Pics 8"
	INCBIN "gfx/pokemon/front/raya.pic", 0, 1 ; sprite dimensions
	dw RayaPicFront, RayaPicBack

	db WRAP, LEER, BITE, NO_MOVE ; level 1 learnset
	db GROWTH_MEDIUM_FAST ; growth rate

	; tm/hm learnset
	tmhm RAZOR_WIND,   TOXIC,        BODY_SLAM,    TAKE_DOWN,    DOUBLE_EDGE,  \
	     BUBBLEBEAM,   WATER_GUN,    ICE_BEAM,     BLIZZARD,     HYPER_BEAM,   \
	     RAGE,         DRAGON_RAGE,  THUNDERBOLT,  THUNDER,      MIMIC,        \
	     DOUBLE_TEAM,  REFLECT,      BIDE,         FIRE_BLAST,   SWIFT,        \
	     SKULL_BASH,   REST,         THUNDER_WAVE, SUBSTITUTE,   SURF,         \
	     STRENGTH
	; end

	db 0 ; padding
