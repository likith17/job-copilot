from scipy.stats import spearmanr

# Jobs in a fixed order
jobs = ["Talentry", "Nuro", "Zoox", "Postman", "Akia"]

# Your expert fit ratings (1-5)
my_ratings = [5, 3, 3, 3, 2]

# The tool's match scores
tool_scores = [0.58, 0.48, 0.38, 0.37, 0.34]

correlation, p_value = spearmanr(my_ratings, tool_scores)

print("Spearman correlation:", round(correlation, 3))
print("p-value:", round(p_value, 3))

