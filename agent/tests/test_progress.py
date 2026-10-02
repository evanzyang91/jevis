"""The plan-progress ledger, from strings alone: no browser, no model."""

from __future__ import annotations

from agent.planner import Plan, Progress, SubGoal, cart_count, is_committing, product_of
from agent.policy.jev_policy import asks_several

CAKE = [("all-purpose flour", "bag"), ("granulated sugar", "bag"), ("eggs", "carton"), ("butter", "pack"),
        ("baking powder", "container"), ("vanilla extract", "bottle")]


def _shop(items: list[tuple[str, str]], constraints: list[str] | None = None) -> Progress:
    steps = [SubGoal(text=f"Search '{term}' and add one {unit}.", check=f"Cart shows {term}.",
                     search_term=term, done_when="add") for term, unit in items]
    plan = Plan(original_goal="Buy me cake ingredients from walmart", refined_goal="(full)",
                start_url="https://www.walmart.ca/", subgoals=steps,
                constraints=constraints if constraints is not None else [
                    "If an item is out of stock, substitute the closest equivalent and continue.",
                    "Do not place the order."])
    return Progress(plan)


def test_the_logged_cake_run_is_tracked_step_by_step() -> None:
    """Run 9ae93ef1's adds, in order. Substitutes finish their own step, an
    item added while another is active finishes its own, repeats finish
    nothing, and the run is not over: baking powder was never added."""
    progress = _shop(CAKE)
    log = [
        ("Add to cart - Great Value Original All-Purpose Flour", [0]),
        ("Add to cart - Great Value Organic Pure Golden Sugar, 900 g", [1]),  # no "granulated" in the name
        ("Add to cart - Gay Lea Unsalted Butter", [3]),  # added while eggs were active
        ("Add to cart - Redpath Special Fine Granulated Sugar", []),  # a second sugar
        ("Add to cart - Conestoga Farms Free Run Omega-3 Large Brown 12 Eggs", [2]),
        ("Add to cart - Conestoga Farms Free Run Omega3 Large 18 Eggs", []),  # a second carton
        ("Add to cart - Great Value Pure Vanilla Extract", [5]),  # added while baking powder was active
    ]
    for label, finished in log:
        assert [change.index for change in progress.on_add(label)] == finished, label
    assert not progress.finished()
    assert progress.active is not None and progress.active.term == "baking powder"
    assert progress.done_count() == 5
    assert len(progress.notes) == 2
    progress.on_add("Add to cart - GoodEats Baking Powder 370g")
    assert progress.finished()
    assert progress.summary().startswith("all 6 steps done")


def test_the_policy_goal_names_one_step_and_carries_no_counts() -> None:
    progress = _shop(CAKE)
    progress.on_add("Add to cart - Great Value Original All-Purpose Flour")
    progress.on_add("Add to cart - Great Value Large 12 Eggs, 1 dozen")  # eggs, out of order
    goal = progress.goal_for_policy()
    assert goal.startswith("Buy me cake ingredients from walmart.")
    assert "Current step: Search 'granulated sugar' and add one bag." in goal
    assert ("Finished steps, never redo them: all-purpose flour (added Great Value Original All-Purpose Flour); "
            "eggs (added Great Value Large Eggs).") in goal
    assert "Later steps, not now: butter; baking powder; vanilla extract." in goal
    assert "Do not place the order." in goal
    # "12", "1 dozen" would make the policy offer quantity controls.
    assert not asks_several(goal), goal


def test_a_more_specific_name_wins_over_the_active_step() -> None:
    progress = _shop([("pasta", "pack"), ("pasta sauce", "jar")])
    assert [c.index for c in progress.on_add("Add to cart - Classico Tomato Basil Pasta Sauce")] == [1]
    assert progress.active is not None and progress.active.term == "pasta"
    assert [c.index for c in progress.on_add("Add to cart - Barilla Spaghetti")] == [0]
    assert progress.finished()


def test_an_add_finishes_the_stage_steps_before_it() -> None:
    """DoorDash: open the store, open the item, add it. The add proves the
    stages before it, and its price-only label takes the dialog's name."""
    steps = [SubGoal(text="Search 'chipotle' and open the nearest Chipotle store.", check="Store page open.",
                     search_term="chipotle", done_when="page"),
             SubGoal(text="Open the burrito bowl and choose barbacoa.", check="Bowl options open.", done_when="page"),
             SubGoal(text="Add one barbacoa bowl to the cart.", check="Cart shows one bowl.", done_when="add")]
    progress = Progress(Plan(original_goal="order me a barbacoa bowl from chipotle", refined_goal="(full)",
                             start_url="https://www.doordash.com/", subgoals=steps))
    changes = progress.on_add("Add to cart - CA$15.60", fallback_name="Burrito Bowl")
    assert [(c.index, c.status) for c in changes] == [(0, "done"), (1, "done"), (2, "done")]
    assert progress.steps[2].product == "Burrito Bowl"
    assert progress.finished()


def test_the_cart_badge_confirms_an_add_late_and_reopens_one_that_never_landed() -> None:
    progress = _shop(CAKE[:2])
    progress.on_cart(11)
    progress.on_add("Add to cart - Great Value Original All-Purpose Flour")
    assert progress.awaiting_cart()
    assert progress.on_cart(11) == []  # Walmart's badge lags one read at times
    assert progress.on_cart(12) == [] and not progress.awaiting_cart()
    progress.on_add("Add to cart - Redpath Granulated Sugar")
    progress.on_cart(12)
    changes = progress.on_cart(12)
    assert [(c.index, c.status) for c in changes] == [(1, "active")]
    assert progress.active is not None and progress.active.term == "granulated sugar"
    # The retry is trusted after the same wait, whatever the badge says: one reopen per step.
    progress.on_add("Add to cart - Redpath Granulated Sugar")
    assert progress.on_cart(12) == [] and progress.on_cart(12) == []
    assert progress.finished() and not progress.awaiting_cart()
    assert progress.summary().endswith("cart 11 -> 12 items")


def test_an_empty_badge_mid_run_is_ignored() -> None:
    progress = _shop(CAKE[:1])
    progress.on_cart(14)
    progress.on_add("Add to cart - Robin Hood Flour")
    assert progress.on_cart(0) == []  # a product page painted "0 items" for one read
    assert progress.on_cart(15) == [] and not progress.awaiting_cart()


def test_a_badge_rise_after_a_plain_click_is_an_add() -> None:
    progress = _shop(CAKE[:2])
    progress.on_cart(3)
    assert [c.index for c in progress.on_cart(4, last_click="Quick add")] == [0]
    # The saved cart loading in after a click is not an add.
    progress = _shop(CAKE[:2])
    progress.on_cart(0)
    assert progress.on_cart(11, last_click="Accept cookies") == []
    # Nor is a quantity control on an item already in the cart.
    assert progress.on_cart(12, last_click="Increase quantity, Great Value Flour") == []


def test_a_search_step_finishes_when_its_query_is_submitted() -> None:
    steps = [SubGoal(text="Search 'transformer attention' on Google Scholar.", check="Results shown.",
                     search_term="transformer attention", done_when="search"),
             SubGoal(text="Open the top result from the past year.", check="Paper page is open.")]
    progress = Progress(Plan(original_goal="find a paper", refined_goal="(full)", start_url="u", subgoals=steps))
    assert progress.on_navigate("https://scholar.google.com/") == []
    progress.on_fill("transformer attention")
    assert [c.index for c in progress.on_navigate("https://scholar.google.com/scholar?q=x")] == [0]
    # The second step completes only through a confirmed DONE.
    assert progress.on_navigate("https://arxiv.org/abs/1") == []
    # A suggestion link that searches the term, without typing, also counts.
    again = Progress(Plan(original_goal="g", refined_goal="g", start_url="u", subgoals=steps[:1]))
    assert again.on_navigate("https://scholar.google.com/scholar?q=transformer+attention&hl=en")


def test_skip_and_rewrite_keep_the_run_going() -> None:
    progress = _shop(CAKE[:3])
    progress.on_add("Add to cart - Robin Hood Flour")
    sugar = progress.active
    assert sugar is not None
    progress.rewrite(sugar, SubGoal(text="Search 'sugar' and add one bag.", check="c", search_term="sugar",
                                    done_when="add"))
    assert progress.active is sugar and sugar.term == "sugar" and sugar.repairs == 1
    assert progress.plan.subgoals[1].search_term == "sugar"
    progress.skip(sugar, "no sugar sold here")
    assert progress.active is not None and progress.active.term == "eggs"
    assert "Skipped steps, never retry them: sugar." in progress.goal_for_policy()
    progress.on_add("Add to cart - Large Eggs")
    assert progress.finished()
    assert progress.summary().startswith("2 of 3 steps done")
    assert "skipped sugar (no sugar sold here)" in progress.summary()
    # A skipped item that gets added after all is credited.
    progress.on_add("Add to cart - Redpath Sugar")
    assert progress.steps[1].status == "done"


def test_the_plan_mirrors_the_active_index() -> None:
    progress = _shop(CAKE[:2])
    assert progress.plan.active_index == 0
    progress.on_add("Add to cart - Five Roses Flour")
    assert progress.plan.active_index == 1
    progress.on_add("Add to cart - Lantic Sugar")
    assert progress.plan.active_index == 2 and progress.plan.completed()
    assert [s["status"] for s in progress.snapshot()] == ["done", "done"]


def test_label_readers() -> None:
    assert is_committing("Add to cart - Great Value Flour")
    assert is_committing("Add item to cart")
    assert is_committing("Add Burrito Bowl to order")
    assert is_committing("Add to Cart")
    assert not is_committing("Add to Favourites list, Great Value Flour")
    assert not is_committing("Sign in to add to Favourites list, Rogers Sugar")
    assert not is_committing("Options - Redpath Special Fine Granulated Sugar")
    assert product_of("Add to cart - Great Value Pure Vanilla Extract") == "Great Value Pure Vanilla Extract"
    assert product_of("Add Burrito Bowl to order") == "Burrito Bowl"
    assert product_of("Add to cart - CA$15.60") == ""
    assert product_of("Add to cart - Rogers Sugar $7.47") == "Rogers Sugar"
    assert cart_count(["Skip to Main Content", "Cart contains 11 items Total Amount $78.34"]) == 11
    assert cart_count(["View cart (15)"]) == 15
    assert cart_count(["1 item in cart"]) == 1
    assert cart_count(["Add to cart - 12 Eggs", "Search"]) is None
